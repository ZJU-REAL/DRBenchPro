"""DRBenchPro Local Research Framework workflow."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .citations import validate_report_citations
from .config import LocalResearchConfig
from .model_client import ChatModel, ModelClientError
from .runtime import RunRecorder, assistant_message, tool_message
from .schemas import ChatResponse, ResearchUnitResult, ToolCall
from .wiki_client import WIKI_TOOL_SCHEMAS, WikiTools, wikipedia_tool_schemas


_CITATION_INSTRUCTION = (
    "Cite claims using the exact local source identifier in double brackets, for "
    "example [[enwiki-20260701-v1:p:123:r:456:s:2]]. "
    "Only cite source_id values returned by tools. Do not cite live web pages."
)


@dataclass
class AgentOutput:
    final_report: str
    research_units: list[ResearchUnitResult]
    metadata: dict[str, Any]


@dataclass
class _FinalReportOutcome:
    response: ChatResponse
    attempts: int
    rejected_reasons: list[str]


class LocalResearchWorkflow:
    def __init__(
        self,
        config: LocalResearchConfig,
        model: ChatModel,
        wiki: WikiTools,
        recorder: RunRecorder,
    ):
        self.config = config
        self.model = model
        self.wiki = wiki
        self.recorder = recorder

    def run(self, query: str) -> AgentOutput:
        self.recorder.event("protocol_started", self.config.protocol, details={"query": query})
        units = self._plan(query)
        results = self._run_units(query, units)

        gap_unit = self._gap_check(query, results)
        if gap_unit is not None and len(results) < self.config.max_research_units:
            gap_result = self._run_unit_safely(query, gap_unit)
            results.append(gap_result)

        grounded_results = [
            result
            for result in results
            if result.status == "success" and result.source_ids and result.notes.strip()
        ]
        all_units_grounded = bool(results) and len(grounded_results) == len(results)
        completion_metadata = {
            "research_unit_count": len(results),
            "grounded_unit_count": len(grounded_results),
            "failed_unit_count": sum(result.status == "error" for result in results),
            "incomplete_unit_count": sum(
                result.status == "incomplete" for result in results
            ),
            "all_units_grounded": all_units_grounded,
            "unit_completion_required": self.config.require_all_units_grounded,
            "partial_evidence_synthesis": bool(
                grounded_results and not all_units_grounded
            ),
            "failed_unit_ids": [
                result.unit_id for result in results if result.status == "error"
            ],
            "incomplete_unit_ids": [
                result.unit_id for result in results if result.status == "incomplete"
            ],
        }
        if not grounded_results:
            self.recorder.event(
                "synthesis_skipped",
                "report_synthesis",
                details={"reason": "no_grounded_research_units"},
            )
            return AgentOutput(
                final_report="",
                research_units=results,
                metadata={
                    "planned_units": units,
                    "gap_unit_added": gap_unit is not None,
                    **completion_metadata,
                    "synthesis_skipped": True,
                    "synthesis_skipped_reason": "no_grounded_research_units",
                    "final_finish_reason": "",
                    "truncated": False,
                },
            )

        if self.config.require_all_units_grounded and not all_units_grounded:
            self.recorder.event(
                "synthesis_skipped",
                "report_synthesis",
                details={
                    "reason": "research_units_not_fully_grounded",
                    **completion_metadata,
                },
            )
            return AgentOutput(
                final_report="",
                research_units=results,
                metadata={
                    "planned_units": units,
                    "gap_unit_added": gap_unit is not None,
                    **completion_metadata,
                    "synthesis_skipped": True,
                    "synthesis_skipped_reason": "research_units_not_fully_grounded",
                    "final_finish_reason": "",
                    "truncated": False,
                },
            )

        final_outcome = self._synthesize(query, grounded_results)
        final_response = final_outcome.response
        final_report = final_response.content
        self.recorder.event(
            "protocol_completed",
            "local_research",
            details={
                "research_unit_count": len(results),
                "final_report_chars": len(final_report),
            },
        )
        return AgentOutput(
            final_report=final_report,
            research_units=results,
            metadata={
                "planned_units": units,
                "gap_unit_added": gap_unit is not None,
                **completion_metadata,
                "synthesis_skipped": False,
                "final_finish_reason": final_response.finish_reason,
                "truncated": _is_truncated_finish_reason(final_response.finish_reason),
                "final_output_attempts": final_outcome.attempts,
                "final_output_rejections": final_outcome.rejected_reasons,
                "final_output_issue": _final_report_issue(
                    final_response, min_words=self.config.report_min_words
                ),
            },
        )

    def _plan(self, query: str) -> list[dict[str, str]]:
        phase = "research_plan"
        messages = [
            {
                "role": "system",
                "content": (
                    "Create one bounded research plan. Return JSON only with this "
                    "schema: {\"units\":[{\"title\":str,\"objective\":str}]}. "
                    f"Produce {self.config.initial_unit_min} to "
                    f"{self.config.initial_unit_max} non-overlapping units. Each unit "
                    "must be answerable from the fixed English Wikipedia snapshot."
                ),
            },
            {"role": "user", "content": query},
        ]
        response = self.model.chat(
            messages,
            tool_choice="none",
            purpose=phase,
            max_tokens=self.config.model_output_tokens,
        )
        self.recorder.record_model(response, phase=phase)
        parsed = _parse_json_object(response.content)
        raw_units = parsed.get("units") if isinstance(parsed, dict) else None
        units: list[dict[str, str]] = []
        if isinstance(raw_units, list):
            for item in raw_units:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "") or "").strip()
                objective = str(item.get("objective", "") or "").strip()
                if title and objective:
                    units.append({"title": title, "objective": objective})
                if len(units) >= self.config.initial_unit_max:
                    break
        fallback = _fallback_units(query)
        while len(units) < self.config.initial_unit_min:
            candidate = fallback[len(units)]
            units.append(candidate)
        for index, unit in enumerate(units, start=1):
            unit["unit_id"] = f"unit-{index:02d}"
        self.recorder.event(
            "plan_created", phase, details={"units": units, "fallback_used": not raw_units}
        )
        return units

    def _run_units(
        self, query: str, units: list[dict[str, str]]
    ) -> list[ResearchUnitResult]:
        if self.config.unit_concurrency <= 1 or len(units) <= 1:
            return [self._run_unit_safely(query, unit) for unit in units]
        ordered: dict[str, ResearchUnitResult] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.config.unit_concurrency, len(units)),
            thread_name_prefix="local-research-unit",
        ) as executor:
            future_to_id = {
                executor.submit(self._run_unit_safely, query, unit): unit["unit_id"]
                for unit in units
            }
            for future in as_completed(future_to_id):
                unit_id = future_to_id[future]
                ordered[unit_id] = future.result()
        return [ordered[unit["unit_id"]] for unit in units]

    def _run_unit_safely(
        self, query: str, unit: dict[str, str]
    ) -> ResearchUnitResult:
        attempts = self.config.unit_retry_attempts + 1
        last_result: ResearchUnitResult | None = None
        for attempt in range(1, attempts + 1):
            result = self._run_unit_once_safely(query, unit)
            result.budget_usage["unit_attempts"] = attempt
            result.budget_usage["unit_retries"] = attempt - 1
            if result.status == "success":
                if attempt > 1:
                    self.recorder.event(
                        "research_unit_recovered",
                        "research_unit",
                        research_unit_id=unit["unit_id"],
                        details={"attempt": attempt, "status": result.status},
                    )
                return result
            last_result = result
            if attempt < attempts:
                self.recorder.event(
                    "research_unit_retry_scheduled",
                    "research_unit",
                    research_unit_id=unit["unit_id"],
                    details={
                        "failed_attempt": attempt,
                        "next_attempt": attempt + 1,
                        "status": result.status,
                        "error": result.error,
                    },
                )
        assert last_result is not None
        return last_result

    def _run_unit_once_safely(
        self, query: str, unit: dict[str, str]
    ) -> ResearchUnitResult:
        try:
            return self._run_unit(query, unit)
        except Exception as exc:
            unit_id = unit["unit_id"]
            if isinstance(exc, ModelClientError):
                purpose = exc.purpose or f"research_unit:{unit_id}"
                phase = "unit_summary" if purpose.startswith("unit_summary:") else "research_unit"
                self.recorder.record_model_error(
                    exc,
                    phase=phase,
                    research_unit_id=unit_id,
                )
            self.recorder.event(
                "research_unit_failed",
                "research_unit",
                research_unit_id=unit_id,
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
            return ResearchUnitResult(
                unit_id=unit_id,
                title=unit["title"],
                objective=unit["objective"],
                notes="",
                rounds=0,
                tool_calls=0,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _run_unit(
        self, query: str, unit: dict[str, Any]
    ) -> ResearchUnitResult:
        unit_id = unit["unit_id"]
        phase = "research_unit"
        max_search = self.config.search_calls_per_unit
        max_open = self.config.open_calls_per_unit
        max_open_turn = self.config.open_calls_per_turn
        max_open_chars = self.config.open_chars_per_unit
        if max_search <= 1:
            search_guidance = "Do not perform an additional search."
        elif max_search == 2:
            search_guidance = (
                "Perform at most one additional search, only to close a material "
                "evidence gap."
            )
        else:
            search_guidance = (
                f"After the initial search, perform up to {max_search - 1} additional "
                "searches when they close material evidence gaps."
            )
        is_gap_unit = bool(unit.get("is_gap_unit"))
        budget_scope = "gap_research_unit" if is_gap_unit else "initial_research_units"
        scope_limit = (
            self.config.gap_unit_tool_limit
            if is_gap_unit
            else self.config.initial_units_tool_limit
        )
        tools = wikipedia_tool_schemas(
            max_open_chars=self.config.open_chars_per_call
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are one bounded research unit in the DRBenchPro Local "
                    "Research Framework. Investigate only "
                    "the assigned objective over the fixed local Wikipedia snapshot. "
                    "Only search and open are available. Start with one search, then "
                    f"open up to {max_open_turn} relevant results in one response. "
                    "Use each search result's suggested_cursor when the matching chunk "
                    "is not at the start of its section. If open returns next_cursor "
                    "and more text is needed, continue that source with exactly that "
                    f"cursor. {search_guidance} Across this unit use at most "
                    f"{max_search} searches, {max_open} "
                    f"opens, {self.config.open_chars_per_call} characters per open, and "
                    f"{max_open_chars} opened characters total. Return concise evidence "
                    "notes, not a full report, only after at least one search and one "
                    f"open succeed. {_CITATION_INSTRUCTION}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Overall question:\n{query}\n\nUnit title: {unit['title']}\n"
                    f"Unit objective:\n{unit['objective']}"
                ),
            },
        ]
        self.recorder.event(
            "research_unit_started",
            phase,
            research_unit_id=unit_id,
            details={
                "title": unit["title"],
                "objective": unit["objective"],
                "budget_scope": budget_scope,
                "budget": {
                    "search_calls": max_search,
                    "open_calls": max_open,
                    "open_calls_per_turn": max_open_turn,
                    "open_chars_per_call": self.config.open_chars_per_call,
                    "open_chars_per_unit": max_open_chars,
                    "model_rounds": self.config.rounds_per_unit,
                    "scope_tool_calls": scope_limit,
                    "emergency_tool_calls": self.config.max_total_tool_calls,
                },
            },
        )

        notes = ""
        rounds = 0
        tool_calls = 0
        search_calls = 0
        open_calls = 0
        successful_search_calls = 0
        successful_open_calls = 0
        opened_chars = 0
        opened_source_ids: list[str] = []
        seen_opened_sources: set[str] = set()
        opened_positions: set[tuple[str, int]] = set()
        failed_open_positions: set[tuple[str, int]] = set()
        continuation_cursors: dict[str, set[int]] = {}
        opened_evidence: list[str] = []

        for round_index in range(1, self.config.rounds_per_unit + 1):
            rounds = round_index
            grounded = successful_search_calls >= 1 and successful_open_calls >= 1
            is_final_unit_round = round_index == self.config.rounds_per_unit and grounded
            if is_final_unit_round:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "This is the final unit round. Summarize only the evidence "
                            "already opened, with exact source_id citations. Do not call "
                            "tools."
                        ),
                    }
                )
            required_tool = (
                "search"
                if successful_search_calls < 1
                else "open"
                if successful_open_calls < 1
                else ""
            )
            response = self.model.chat(
                messages,
                tools=tools,
                tool_choice=(
                    "none"
                    if is_final_unit_round
                    else _required_tool_choice(required_tool)
                    if required_tool
                    else "auto"
                ),
                purpose=(
                    f"unit_summary:{unit_id}"
                    if is_final_unit_round
                    else f"{phase}:{unit_id}"
                ),
                max_tokens=(self.config.summary_output_tokens if is_final_unit_round
                            else self.config.research_output_tokens),
            )
            self.recorder.record_model(
                response,
                phase="unit_summary" if is_final_unit_round else phase,
                round_index=round_index,
                research_unit_id=unit_id,
            )
            messages.append(assistant_message(response))
            if is_final_unit_round:
                notes = response.content
                break

            if response.tool_calls:
                open_calls_this_turn = 0
                for call in response.tool_calls:
                    open_position: tuple[str, int] | None = None
                    if call.name not in {"search", "open"}:
                        _reject_tool_call(
                            self.recorder,
                            messages,
                            call,
                            phase=phase,
                            round_index=round_index,
                            unit_id=unit_id,
                            reason="tool_not_exposed",
                            message="The Local Research Framework exposes only search and open.",
                        )
                        continue
                    if required_tool == "search" and call.name != "search":
                        _reject_tool_call(
                            self.recorder,
                            messages,
                            call,
                            phase=phase,
                            round_index=round_index,
                            unit_id=unit_id,
                            reason="required_tool_mismatch",
                            message="A successful search is required before open.",
                        )
                        continue
                    if (
                        required_tool == "search"
                        and successful_search_calls >= 1
                    ):
                        _reject_tool_call(
                            self.recorder,
                            messages,
                            call,
                            phase=phase,
                            round_index=round_index,
                            unit_id=unit_id,
                            reason="required_tool_already_satisfied",
                            message="The required search already succeeded this round.",
                        )
                        continue
                    if required_tool == "open" and call.name != "open":
                        _reject_tool_call(
                            self.recorder,
                            messages,
                            call,
                            phase=phase,
                            round_index=round_index,
                            unit_id=unit_id,
                            reason="required_tool_mismatch",
                            message=(
                                "At least one open must succeed before another search."
                            ),
                        )
                        continue
                    if call.name == "search" and search_calls >= max_search:
                        _reject_tool_call(
                            self.recorder,
                            messages,
                            call,
                            phase=phase,
                            round_index=round_index,
                            unit_id=unit_id,
                            reason="unit_search_budget_exhausted",
                            message="This unit has used its search budget.",
                        )
                        continue
                    if call.name == "open":
                        if open_calls >= max_open:
                            _reject_tool_call(
                                self.recorder,
                                messages,
                                call,
                                phase=phase,
                                round_index=round_index,
                                unit_id=unit_id,
                                reason="unit_open_budget_exhausted",
                                message="This unit has used its open-call budget.",
                            )
                            continue
                        if open_calls_this_turn >= max_open_turn:
                            _reject_tool_call(
                                self.recorder,
                                messages,
                                call,
                                phase=phase,
                                round_index=round_index,
                                unit_id=unit_id,
                                reason="turn_open_budget_exhausted",
                                message="This response already used its open-call limit.",
                            )
                            continue
                        if max_open_chars - opened_chars < 128:
                            _reject_tool_call(
                                self.recorder,
                                messages,
                                call,
                                phase=phase,
                                round_index=round_index,
                                unit_id=unit_id,
                                reason="unit_open_char_budget_exhausted",
                                message="This unit has used its opened-character budget.",
                            )
                            continue

                    effective_call = call
                    if call.name == "open" and not call.parse_error:
                        source_id = str(call.arguments.get("source_id", "") or "").strip()
                        try:
                            cursor = max(0, int(call.arguments.get("cursor", 0) or 0))
                        except (TypeError, ValueError):
                            cursor = -1
                        position = (source_id, cursor)
                        open_position = position
                        if position in failed_open_positions:
                            _reject_tool_call(
                                self.recorder,
                                messages,
                                call,
                                phase=phase,
                                round_index=round_index,
                                unit_id=unit_id,
                                reason="repeated_failed_open_position",
                                message=(
                                    "This source and cursor already failed to open. "
                                    "Choose a different source_id returned by search."
                                ),
                            )
                            continue
                        allowed_continuations = continuation_cursors.get(source_id, set())
                        if position in opened_positions:
                            _reject_tool_call(
                                self.recorder,
                                messages,
                                call,
                                phase=phase,
                                round_index=round_index,
                                unit_id=unit_id,
                                reason="duplicate_open_position",
                                message="This source and cursor were already opened.",
                            )
                            continue
                        if source_id in seen_opened_sources and cursor not in allowed_continuations:
                            _reject_tool_call(
                                self.recorder,
                                messages,
                                call,
                                phase=phase,
                                round_index=round_index,
                                unit_id=unit_id,
                                reason="invalid_open_continuation",
                                message=(
                                    "Reopen a source only with a next_cursor returned "
                                    "by its previous open."
                                ),
                            )
                            continue
                        remaining_chars = max_open_chars - opened_chars
                        try:
                            requested_chars = int(
                                call.arguments.get(
                                    "max_chars", self.config.open_chars_per_call
                                )
                                or self.config.open_chars_per_call
                            )
                        except (TypeError, ValueError):
                            requested_chars = self.config.open_chars_per_call
                        effective_arguments = {
                            **call.arguments,
                            "cursor": cursor,
                            "max_chars": min(
                                self.config.open_chars_per_call,
                                remaining_chars,
                                max(128, requested_chars),
                            ),
                        }
                        effective_call = ToolCall(
                            id=call.id,
                            name=call.name,
                            arguments=effective_arguments,
                            arguments_raw=json.dumps(
                                effective_arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )

                    if not self.recorder.reserve_tool_call(
                        call.name,
                        total_limit=self.config.max_total_tool_calls,
                        scope=budget_scope,
                        scope_limit=scope_limit,
                    ):
                        _reject_tool_call(
                            self.recorder,
                            messages,
                            call,
                            phase=phase,
                            round_index=round_index,
                            unit_id=unit_id,
                            reason="tool_budget_exhausted",
                            message=(
                                "The initial/gap safety budget or emergency tool "
                                "ceiling is exhausted."
                            ),
                        )
                        continue

                    output, success, returned_sources = self.recorder.execute_tool(
                        self.wiki,
                        effective_call,
                        phase=phase,
                        round_index=round_index,
                        research_unit_id=unit_id,
                    )
                    tool_calls += 1
                    if call.name == "search":
                        search_calls += 1
                        if success and returned_sources:
                            successful_search_calls += 1
                    else:
                        open_calls += 1
                        open_calls_this_turn += 1
                        if success and returned_sources:
                            successful_open_calls += 1
                            content = str(output.get("content", "") or "")
                            opened_chars += len(content)
                            source_id = str(output.get("source_id", "") or "").strip()
                            cursor = int(output.get("cursor", 0) or 0)
                            if source_id:
                                opened_positions.add((source_id, cursor))
                                seen_opened_sources.add(source_id)
                                if source_id not in opened_source_ids:
                                    opened_source_ids.append(source_id)
                                continuation_cursors.setdefault(source_id, set()).discard(cursor)
                                next_cursor = output.get("next_cursor")
                                if isinstance(next_cursor, int):
                                    continuation_cursors.setdefault(source_id, set()).add(
                                        next_cursor
                                    )
                            evidence = _opened_evidence_note(output)
                            if evidence:
                                opened_evidence.append(evidence)
                        elif open_position is not None:
                            failed_open_positions.add(open_position)
                    messages.append(tool_message(effective_call, output))
                continue

            if response.content and grounded:
                notes = response.content
                break
            if response.content:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Complete at least one successful search and one successful "
                            "open before writing evidence notes."
                        ),
                    }
                )

        grounded = successful_search_calls >= 1 and successful_open_calls >= 1
        if grounded and not notes.strip():
            notes = "\n\n".join(opened_evidence).strip()
            if notes:
                self.recorder.event(
                    "research_unit_notes_fallback",
                    "unit_summary",
                    research_unit_id=unit_id,
                    details={
                        "reason": "model_returned_empty_visible_notes",
                        "evidence_block_count": len(opened_evidence),
                        "notes_chars": len(notes),
                    },
                )
        budget_usage = {
            "search_calls": search_calls,
            "successful_search_calls": successful_search_calls,
            "open_calls": open_calls,
            "successful_open_calls": successful_open_calls,
            "open_chars": opened_chars,
            "tool_calls": tool_calls,
        }
        result = ResearchUnitResult(
            unit_id=unit_id,
            title=unit["title"],
            objective=unit["objective"],
            notes=notes if grounded else "",
            rounds=rounds,
            tool_calls=tool_calls,
            source_ids=opened_source_ids,
            status="success" if grounded else "incomplete",
            error=(
                "Research unit did not complete grounded search and open evidence."
                if not grounded
                else ""
            ),
            budget_usage=budget_usage,
        )
        self.recorder.event(
            "research_unit_completed",
            phase,
            research_unit_id=unit_id,
            details=result.to_dict(),
        )
        return result


    def _gap_check(
        self, query: str, results: list[ResearchUnitResult]
    ) -> dict[str, str] | None:
        if not self.config.gap_check_enabled:
            return None
        if len(results) >= self.config.max_research_units:
            return None
        phase = "gap_check"
        evidence = _render_unit_evidence(results, max_chars=self.config.gap_evidence_chars)
        messages = [
            {
                "role": "system",
                "content": (
                    "Perform exactly one coverage-gap check. Return JSON only: "
                    "{\"needs_more_research\":bool,\"unit\":{\"title\":str,"
                    "\"objective\":str}}. Add a unit only for a material missing "
                    "branch that can be researched in Wikipedia."
                ),
            },
            {"role": "user", "content": f"Question:\n{query}\n\nEvidence:\n{evidence}"},
        ]
        response = self.model.chat(
            messages,
            tool_choice="none",
            purpose=phase,
            max_tokens=self.config.model_output_tokens,
        )
        self.recorder.record_model(response, phase=phase)
        parsed = _parse_json_object(response.content)
        if not isinstance(parsed, dict) or parsed.get("needs_more_research") is not True:
            self.recorder.event("gap_check_completed", phase, details={"unit_added": False})
            return None
        item = parsed.get("unit")
        if not isinstance(item, dict):
            return None
        title = str(item.get("title", "") or "").strip()
        objective = str(item.get("objective", "") or "").strip()
        if not title or not objective:
            return None
        unit = {
            "unit_id": f"unit-{len(results) + 1:02d}",
            "title": title,
            "objective": objective,
            "is_gap_unit": True,
        }
        self.recorder.event(
            "gap_check_completed", phase, details={"unit_added": True, "unit": unit}
        )
        return unit

    def _synthesize(
        self, query: str, results: list[ResearchUnitResult]
    ) -> _FinalReportOutcome:
        phase = "report_synthesis"
        evidence = _render_unit_evidence(
            results, max_chars=self.config.synthesis_evidence_chars
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "Synthesize a complete, coherent Markdown research report from "
                    "the research-unit evidence. Do not invent facts or sources. "
                    "Resolve overlap and clearly state limitations. "
                    f"{_CITATION_INSTRUCTION} "
                    f"{str(self.config.report.get('system_prompt', '') or '').strip()} "
                    f"Write at least {self.config.report_min_words} words."
                ),
            },
            {"role": "user", "content": f"Question:\n{query}\n\nEvidence:\n{evidence}"},
        ]
        return _generate_final_report(
            config=self.config,
            model=self.model,
            recorder=self.recorder,
            messages=messages,
            phase=phase,
            tools=wikipedia_tool_schemas(
                max_open_chars=self.config.open_chars_per_call
            ),
            allowed_source_ids=list(self.recorder.tool_usage.retrieved_source_ids),
        )


def _generate_final_report(
    *,
    config: LocalResearchConfig,
    model: ChatModel,
    recorder: RunRecorder,
    messages: list[dict[str, Any]],
    phase: str,
    round_index_start: int = 1,
    tools: list[dict[str, Any]] | None = None,
    allowed_source_ids: list[str] | None = None,
) -> _FinalReportOutcome:
    rejected_reasons: list[str] = []
    last_response: ChatResponse | None = None
    retry_messages = list(messages)
    for attempt in range(1, config.final_report_attempts + 1):
        response = model.chat(
            retry_messages,
            tools=tools or WIKI_TOOL_SCHEMAS,
            tool_choice="none",
            purpose=phase,
            max_tokens=config.final_report_tokens,
        )
        recorder.record_model(
            response,
            phase=phase,
            round_index=round_index_start + attempt - 1,
        )
        last_response = response
        issue = _final_report_issue(response, min_words=config.report_min_words)
        citation_validation = None
        if not issue and config.citation_required:
            citation_validation = validate_report_citations(
                response.content,
                list(allowed_source_ids or []),
                citation_required=True,
            )
            if not citation_validation.valid:
                issue = (
                    "invalid_citations"
                    if citation_validation.invalid_source_ids
                    else "missing_citations"
                )
        if not issue:
            if rejected_reasons:
                recorder.event(
                    "final_report_recovered",
                    phase,
                    round_index=round_index_start + attempt - 1,
                    details={
                        "attempt": attempt,
                        "prior_rejections": list(rejected_reasons),
                    },
                )
            return _FinalReportOutcome(
                response=response,
                attempts=attempt,
                rejected_reasons=rejected_reasons,
            )
        rejected_reasons.append(issue)
        recorder.event(
            "final_report_rejected",
            phase,
            round_index=round_index_start + attempt - 1,
            details={
                "attempt": attempt,
                "max_attempts": config.final_report_attempts,
                "reason": issue,
                "content_chars": len(response.content),
                "word_count": len(response.content.split()),
                "finish_reason": response.finish_reason,
                "tool_call_count": len(response.tool_calls),
                **(
                    {"citation_validation": citation_validation.to_dict()}
                    if citation_validation is not None
                    else {}
                ),
            },
        )
        if attempt < config.final_report_attempts:
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": _final_report_retry_instruction(
                        issue,
                        min_words=config.report_min_words,
                        allowed_source_ids=list(allowed_source_ids or []),
                    ),
                },
            ]
    if last_response is None:  # pragma: no cover - bounded config is always >= 1
        raise RuntimeError("Final report generation made no model attempt.")
    return _FinalReportOutcome(
        response=last_response,
        attempts=config.final_report_attempts,
        rejected_reasons=rejected_reasons,
    )


def _final_report_issue(response: ChatResponse, *, min_words: int) -> str:
    finish_reason = str(response.finish_reason or "").strip().lower()
    if response.tool_calls or finish_reason in {"tool_call", "tool_calls"}:
        return "unexpected_tool_calls"
    if not response.content.strip():
        return "empty_visible_content"
    if _is_truncated_finish_reason(finish_reason):
        return "truncated"
    if len(response.content.split()) < min_words:
        return "below_min_words"
    return ""


def _final_report_retry_instruction(
    issue: str,
    *,
    min_words: int,
    allowed_source_ids: list[str] | None = None,
) -> str:
    reason = {
        "unexpected_tool_calls": "You attempted to call a tool during final synthesis.",
        "empty_visible_content": "You returned no visible final report.",
        "truncated": "Your previous final report was truncated.",
        "below_min_words": "Your previous final report was below the required length.",
        "missing_citations": "Your previous final report contained no valid local source citations.",
        "invalid_citations": "Your previous final report cited one or more source IDs that were not returned by the local tools.",
    }.get(issue, "Your previous final report was not acceptable.")
    allowed = ", ".join(dict.fromkeys(allowed_source_ids or []))
    citation_constraint = (
        f" Cite only these exact source IDs: {allowed}."
        if allowed
        else " Do not invent a source ID."
    )
    return (
        f"{reason} Retry the final answer now. Return only one complete Markdown "
        "report using the evidence already supplied. Do not call tools, do not emit "
        "analysis-only text, and do not mention this retry. "
        f"The report must contain at least {min_words} words and exact source_id citations."
        f"{citation_constraint}"
    )


def _required_tool_choice(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def _reject_tool_call(
    recorder: RunRecorder,
    messages: list[dict[str, Any]],
    call: ToolCall,
    *,
    phase: str,
    round_index: int,
    unit_id: str,
    reason: str,
    message: str,
) -> None:
    recorder.event(
        "tool_rejected",
        phase,
        round_index=round_index,
        research_unit_id=unit_id,
        details={
            "tool_call_id": call.id,
            "name": call.name,
            "reason": reason,
        },
    )
    messages.append(
        tool_message(
            call,
            {
                "error": reason,
                "message": message,
            },
        )
    )


def _is_truncated_finish_reason(value: Any) -> bool:
    return str(value or "").strip().lower() in {"length", "max_tokens"}


def _parse_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(value[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def _fallback_units(query: str) -> list[dict[str, str]]:
    return [
        {
            "title": "Core concepts and factual foundations",
            "objective": f"Establish definitions, entities, and foundational facts needed to answer: {query}",
        },
        {
            "title": "Historical development and causal context",
            "objective": f"Research chronology, causes, changes, and important context for: {query}",
        },
        {
            "title": "Comparisons, consequences, and limitations",
            "objective": f"Research comparisons, consequences, disputes, and limitations relevant to: {query}",
        },
        {
            "title": "Key people, organizations, and relationships",
            "objective": f"Identify important actors and relationships that materially affect: {query}",
        },
        {
            "title": "Synthesis checks and edge cases",
            "objective": f"Check notable exceptions and edge cases needed for a balanced answer to: {query}",
        },
    ]


def _render_unit_evidence(
    results: list[ResearchUnitResult], *, max_chars: int
) -> str:
    blocks: list[str] = []
    used = 0
    for result in results:
        grounded = (
            result.status == "success"
            and bool(result.source_ids)
            and bool(result.notes.strip())
        )
        notes = result.notes if grounded else "No grounded evidence returned."
        block = (
            f"## {result.unit_id}: {result.title}\n"
            f"Objective: {result.objective}\n"
            f"Status: {result.status}\n"
            f"Sources: {', '.join(result.source_ids) or 'none'}\n"
            f"Notes:\n{notes}\n"
        )
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 0:
                blocks.append(block[:remaining])
            break
        blocks.append(block)
        used += len(block)
    return "\n".join(blocks)


def _opened_evidence_note(output: dict[str, Any]) -> str:
    source_id = str(output.get("source_id", "") or "").strip()
    content = str(output.get("content", "") or "").strip()
    if not source_id or not content:
        return ""
    return f"Evidence from [[{source_id}]]:\n{content}"
