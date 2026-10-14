"""Public engine API for the DRBenchPro Local Research Framework."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .agents import LocalResearchWorkflow
from .citations import validate_report_citations
from .config import LocalResearchConfig
from .model_client import (
    ChatModel,
    ModelClientError,
    OpenAICompatibleModelClient,
    RoutedModelClient,
    UnavailableModelClient,
)
from .runtime import RunRecorder, TelemetryObserver
from .schemas import ResearchResult, utc_now_iso
from .wiki_client import WikiToolClient, WikiTools
from .version import FRAMEWORK_VERSION


class LocalResearchEngine:
    """Run the DRBenchPro Local Research Framework protocol.

    ``model_client`` and ``wiki_client`` are injectable so protocol behavior can
    be tested without network access. Production construction uses the ordered
    ``model_routes`` and ``wiki`` mappings in the configuration.
    """

    def __init__(
        self,
        config: LocalResearchConfig | dict[str, Any],
        *,
        model_client: ChatModel | None = None,
        wiki_client: WikiTools | None = None,
        telemetry_observer: TelemetryObserver | None = None,
    ):
        self.config = (
            config
            if isinstance(config, LocalResearchConfig)
            else LocalResearchConfig.from_mapping(config)
        )
        self.model = model_client or self._build_model_client()
        self.telemetry_observer = telemetry_observer
        self.wiki = wiki_client or WikiToolClient(
            self.config.wiki,
            snapshot_id=self.config.snapshot_id,
            index_id=self.config.index_id,
            allow_incomplete_index=self.config.allow_incomplete_index,
        )

    def run(self, query: str, case_id: str) -> ResearchResult:
        query = str(query or "").strip()
        case_id = str(case_id or "").strip()
        if not query:
            raise ValueError("query must be non-empty.")
        if not case_id:
            raise ValueError("case_id must be non-empty.")

        started_at = utc_now_iso()
        started = time.perf_counter()
        run_id = f"{case_id}-{uuid.uuid4().hex}"
        trace_id = uuid.uuid4().hex
        recorder = RunRecorder(
            protocol=self.config.protocol,
            observer=self.telemetry_observer,
        )
        final_report = ""
        research_units = []
        error: dict[str, Any] | None = None
        agent_metadata: dict[str, Any] = {}
        index_validation: dict[str, Any] = {}
        resolved_index_id = self.config.index_id
        status = "success"
        try:
            validate_index = getattr(self.wiki, "validate_index", None)
            if callable(validate_index):
                index_validation = dict(validate_index())
                resolved_index_id = str(
                    index_validation.get("index_id", "") or ""
                ).strip()
                if not resolved_index_id:
                    raise ValueError("Validated Wikipedia manifest is missing index_id.")
                recorder.event(
                    "index_validated",
                    "engine",
                    details=index_validation,
                )
            output = LocalResearchWorkflow(
                self.config, self.model, self.wiki, recorder
            ).run(query)
            final_report = output.final_report
            research_units = output.research_units
            agent_metadata = output.metadata
            agent_metadata["framework_version"] = FRAMEWORK_VERSION
            if int(agent_metadata.get("grounded_unit_count", 0) or 0) == 0:
                status = "error"
                error = {
                    "stage": "research",
                    "error_type": "InsufficientToolCoverage",
                    "error_message": (
                        "The Local Research Framework produced no research unit with "
                        "grounded search and "
                        "open evidence; synthesis was skipped."
                    ),
                }
            elif (
                agent_metadata.get("unit_completion_required") is True
                and agent_metadata.get("all_units_grounded") is not True
            ):
                status = "error"
                error = {
                    "stage": "research",
                    "error_type": "IncompleteResearchUnits",
                    "error_message": (
                        "The Local Research Framework did not ground every planned "
                        "Research Unit after "
                        "bounded Unit recovery; partial-evidence synthesis was skipped."
                    ),
                    "grounded_unit_count": int(
                        agent_metadata.get("grounded_unit_count", 0) or 0
                    ),
                    "research_unit_count": int(
                        agent_metadata.get("research_unit_count", 0) or 0
                    ),
                }
            elif agent_metadata.get("final_output_issue") == "unexpected_tool_calls":
                status = "error"
                error = {
                    "stage": "final_report",
                    "error_type": "UnexpectedFinalToolCalls",
                    "error_message": (
                        "Final synthesis still returned tool calls after all bounded "
                        "tool-disabled attempts."
                    ),
                }
            elif not final_report.strip():
                status = "completed_empty"
            elif bool(agent_metadata.get("truncated")):
                status = "truncated"
                agent_metadata["deferred"] = True
                agent_metadata["deferred_reason"] = "finish_reason_length"
            elif len(final_report.split()) < self.config.report_min_words:
                status = "error"
                error = {
                    "stage": "final_report",
                    "error_type": "FinalReportTooShort",
                    "error_message": (
                        "Final report is below the configured minimum word count: "
                        f"{len(final_report.split())}<{self.config.report_min_words}."
                    ),
                }
        except Exception as exc:
            if isinstance(exc, ModelClientError):
                recorder.record_model_error(
                    exc,
                    phase=exc.purpose or "research",
                )
            status = "error"
            error = {
                "stage": "research",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            recorder.event("run_failed", "engine", details=error)

        latency = time.perf_counter() - started
        raw_response = recorder.raw_response()
        raw_response["agent_metadata"] = agent_metadata
        raw_response["index_validation"] = index_validation
        citation_validation = validate_report_citations(
            final_report,
            recorder.tool_usage.retrieved_source_ids,
            citation_required=self.config.citation_required,
        )
        if status == "success" and not citation_validation.valid:
            status = "error"
            error = {
                "stage": "citation_validation",
                "error_type": "InvalidCitations",
                "error_message": (
                    "Final report is missing required local source citations or cites "
                    "source IDs that were not returned by tools."
                ),
            }
        recorder.event(
            "citation_validation",
            "engine",
            details=citation_validation.to_dict(),
        )
        return ResearchResult(
            contract_version=self.config.contract_version,
            status=status,
            protocol=self.config.protocol,
            case_id=case_id,
            query=query,
            snapshot_id=self.config.snapshot_id,
            index_id=resolved_index_id,
            final_report=final_report,
            raw_response=raw_response,
            usage=recorder.usage.copy(),
            tool_usage=recorder.tool_usage,
            trace=list(recorder.trace),
            citation_validation=citation_validation,
            route=recorder.route_summary(),
            stage_metrics=dict(recorder.stage_metrics),
            latency_seconds=latency,
            started_at_utc=started_at,
            finished_at_utc=utc_now_iso(),
            run_id=run_id,
            trace_id=trace_id,
            research_units=research_units,
            error=error,
        )

    def _build_model_client(self) -> ChatModel:
        route_configs = self.config.model_routes
        if not route_configs:
            if not self.config.model:
                raise ValueError(
                    "Provide model or an ordered model_routes list in local research config."
                )
            route_configs = [self.config.model]
        clients: list[ChatModel] = []
        for route in route_configs:
            try:
                clients.append(OpenAICompatibleModelClient(route))
            except ValueError as exc:
                if "api_key is empty" not in str(exc):
                    raise
                clients.append(
                    UnavailableModelClient(
                        source=str(route.get("source", "model")),
                        reason=str(exc),
                    )
                )
        if len(clients) == 1:
            return clients[0]
        return RoutedModelClient(
            clients,
            fallback_policy=self.config.fallback_policy,
        )


def run_research_request(
    payload: dict[str, Any],
    *,
    model_client: ChatModel | None = None,
    wiki_client: WikiTools | None = None,
    telemetry_observer: TelemetryObserver | None = None,
) -> dict[str, Any]:
    """Convenience adapter for a ``POST /v1/research`` JSON payload."""

    if not isinstance(payload, dict):
        raise TypeError("Research request payload must be an object.")
    if str(payload.get("schema_version", "") or "") != "local-wikipedia-research/v1":
        raise ValueError(
            "schema_version must be 'local-wikipedia-research/v1'."
        )
    knowledge_base = payload.get("knowledge_base")
    if not isinstance(knowledge_base, dict):
        raise ValueError("knowledge_base must be an object.")
    for field in ("snapshot_id", "index_id"):
        if not str(knowledge_base.get(field, "") or "").strip():
            raise ValueError(f"knowledge_base.{field} must be non-empty.")
    engine_options = payload.get("engine_options")
    if not isinstance(engine_options, dict) or engine_options.get("offline_search_only") is not True:
        raise ValueError("engine_options.offline_search_only must be true.")
    engine = LocalResearchEngine(
        payload,
        model_client=model_client,
        wiki_client=wiki_client,
        telemetry_observer=telemetry_observer,
    )
    result = engine.run(
        query=str(payload.get("query", "") or ""),
        case_id=str(payload.get("case_id", "") or ""),
    )
    return result.to_api_response()
