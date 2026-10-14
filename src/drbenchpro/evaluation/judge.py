"""LLM-based report scoring against rubric objects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from drbenchpro.llm.client import LLMConfig, OpenAIChatClient, parse_first_json_object
from drbenchpro.common.resources import load_prompt_spec


FINAL_SCORE_ONLY_HALF_PROMPT_FILE = "report_scoring_final_score_only-half.md"
FINAL_SCORE_REASON_HALF_PROMPT_FILE = "report_scoring_final_score_reason-half.md"
FINAL_OUTPUT_FORMAT_PROMPTS = {
    "score_only_half": FINAL_SCORE_ONLY_HALF_PROMPT_FILE,
    "score_half": FINAL_SCORE_ONLY_HALF_PROMPT_FILE,
    "score_reason_half": FINAL_SCORE_REASON_HALF_PROMPT_FILE,
    "score_reason_half_credit": FINAL_SCORE_REASON_HALF_PROMPT_FILE,
}
_CLIENT_CACHE: dict[tuple[str, str, str, float, bool, float, int, float, bool, str, str, bool], OpenAIChatClient] = {}


def evaluate_rubric_final_llm(
    query: str,
    report_text: str,
    rubric_object: dict[str, Any],
    llm_settings: dict[str, Any],
    call_context: dict[str, Any] | None = None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    output_format: str = "score_only_half",
) -> dict[str, Any]:
    """Evaluate one rubric with half-credit scores and optional reasons."""
    prompt_file = _prompt_file_for_final_output_format(output_format)
    return _evaluate_single_rubric(
        prompt_file=prompt_file,
        query=query,
        report_text=report_text,
        rubric_object=rubric_object,
        llm_settings=llm_settings,
        call_context=call_context,
        attempt_hook=attempt_hook,
        output_format=output_format,
    )


def evaluate_rubrics_final_llm(
    query: str,
    report_text: str,
    rubric_objects: list[dict[str, Any]],
    llm_settings: dict[str, Any],
    call_context: dict[str, Any] | None = None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    output_format: str = "score_only_half",
) -> dict[str, Any]:
    """Evaluate a full rubric set in one final-mode LLM call."""
    prompt_file = _prompt_file_for_final_output_format(output_format)
    client = _get_cached_client(llm_settings)
    prompt_spec = load_prompt_spec(prompt_file)
    judge_rubric_objects = [_compact_rubric_for_judge(rubric) for rubric in rubric_objects]
    messages = _build_final_judge_messages(
        system_prompt=prompt_spec.system_prompt,
        query=query.strip(),
        rubric_objects=judge_rubric_objects,
        report_text=report_text,
        cache_report=_should_cache_report_block(llm_settings),
        output_format=output_format,
    )

    raw, meta_out = client.chat(
        messages,
        task_type="judge",
        call_context=call_context or {},
        return_meta=True,
        extra_body=llm_settings.get("extra_body") if isinstance(llm_settings.get("extra_body"), dict) else None,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="judge_batch",
                rubric_count=len(rubric_objects),
            ))
            if attempt_hook is not None
            else None
        ),
    )
    record_path = str(meta_out.get("record_path", ""))
    cache_path = str(meta_out.get("cache_path", ""))
    try:
        obj = parse_first_json_object(raw)
    except Exception as exc:
        client.annotate_record(record_path=record_path, parsed_object=None, parse_error=str(exc))
        _invalidate_cache_path(cache_path)
        raise
    try:
        _validate_final_batch_payload(obj, rubric_objects, output_format=output_format)
    except Exception as exc:
        client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=str(exc))
        _invalidate_cache_path(cache_path)
        raise
    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)
    return _normalize_final_batch_result(obj, rubric_objects, output_format=output_format)


def _evaluate_single_rubric(
    prompt_file: str,
    query: str,
    report_text: str,
    rubric_object: dict[str, Any],
    llm_settings: dict[str, Any],
    call_context: dict[str, Any] | None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    output_format: str = "score_only_half",
) -> dict[str, Any]:
    """Request half-credit scoring for one rubric."""
    client = _get_cached_client(llm_settings)
    prompt_spec = load_prompt_spec(prompt_file)

    messages = _build_final_judge_messages(
        system_prompt=prompt_spec.system_prompt,
        query=query.strip(),
        rubric_objects=[_compact_rubric_for_judge(rubric_object)],
        report_text=report_text,
        cache_report=_should_cache_report_block(llm_settings),
        output_format=output_format,
    )

    raw, meta_out = client.chat(
        messages,
        task_type="judge",
        call_context=call_context or {},
        return_meta=True,
        extra_body=llm_settings.get("extra_body") if isinstance(llm_settings.get("extra_body"), dict) else None,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="judge_single",
                rubric_id=str(rubric_object.get("rubric_id", "")).strip(),
            ))
            if attempt_hook is not None
            else None
        ),
    )
    record_path = str(meta_out.get("record_path", ""))
    cache_path = str(meta_out.get("cache_path", ""))
    try:
        obj = parse_first_json_object(raw)
    except Exception as exc:
        client.annotate_record(record_path=record_path, parsed_object=None, parse_error=str(exc))
        _invalidate_cache_path(cache_path)
        raise
    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)

    return _normalize_final_result(obj, rubric_object, output_format=output_format)


def _prompt_file_for_final_output_format(output_format: str) -> str:
    """Resolve the formal final scoring prompt for the requested response schema."""
    normalized = str(output_format or "score_only_half").strip().lower()
    prompt_file = FINAL_OUTPUT_FORMAT_PROMPTS.get(normalized)
    if prompt_file is None:
        supported = ", ".join(sorted(FINAL_OUTPUT_FORMAT_PROMPTS))
        raise ValueError(f"Unsupported final judge output_format={output_format!r}; supported: {supported}.")
    return prompt_file


def _compact_rubric_for_judge(rubric_object: dict[str, Any]) -> dict[str, str]:
    return {
        "rubric_id": str(rubric_object.get("rubric_id", "")).strip(),
        "rubric_text": str(rubric_object.get("rubric_text", "")).strip(),
    }


def _build_final_judge_messages(
    *,
    system_prompt: str,
    query: str,
    rubric_objects: list[dict[str, str]],
    report_text: str,
    cache_report: bool,
    output_format: str,
) -> list[dict[str, Any]]:
    """Build final-scoring messages so chunks for one report can share provider cache.

    The report is the large stable block across rubric chunks. Put it before the
    dynamic rubric chunk and optionally mark it with OpenRouter/Gemini
    ``cache_control`` so repeated chunk calls can reuse report-token processing.
    """
    if str(output_format or "").strip().lower() in {"score_reason_half", "score_reason_half_credit"}:
        # Split the trusted template before substitution so input text cannot
        # introduce message boundaries. Joining the three user messages with
        # two newlines reconstructs the rendered user template.
        template = load_prompt_spec(FINAL_SCORE_REASON_HALF_PROMPT_FILE).user_prompt_template
        query_template, tail = template.split("\n\n# Report To Evaluate\n", 1)
        report_template, rubric_template = tail.split("\n\nrubric_objects:\n", 1)
        values = {
            "query": query.strip(),
            "report_text": report_text.strip(),
            "rubric_objects_json": json.dumps(rubric_objects, ensure_ascii=False, indent=2),
        }
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query_template.format(**values)},
            {"role": "user", "content": _cacheable_text_content(
                "# Report To Evaluate\n" + report_template.format(**values), enabled=cache_report)},
            {"role": "user", "content": "rubric_objects:\n" + rubric_template.format(**values)},
        ]
    result_fields = "`rubric_id` and `score`"
    query_prompt = (
        "Please formally score a report against a provided rubric set.\n\n"
        "query:\n"
        f"{query.strip()}\n\n"
        "The report to evaluate is provided in the next message. "
        "The rubric_objects to score are provided after the report. "
        "When asked to respond, return only JSON. Each rubric result must contain only "
        f"{result_fields}."
    )
    report_prompt = "# Report To Evaluate\n" + report_text.strip()
    rubric_prompt = (
        "rubric_objects:\n"
        f"{json.dumps(rubric_objects, ensure_ascii=False, indent=2)}\n\n"
        "Now evaluate the report against exactly the rubric object(s) listed above.\n"
        "Use only the provided rubric ids.\n"
        f"{_final_score_instruction(output_format)}"
        "\n"
        "\n"
        "Return ONLY JSON."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query_prompt},
        {"role": "user", "content": _cacheable_text_content(report_prompt, enabled=cache_report)},
        {"role": "user", "content": rubric_prompt},
    ]


def _cacheable_text_content(text: str, *, enabled: bool) -> str | list[dict[str, Any]]:
    """Return a normal text message or an OpenRouter cache-control content block."""
    if not enabled:
        return text
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _should_cache_report_block(llm_settings: dict[str, Any]) -> bool:
    """Whether to emit provider cache hints for the large report message."""
    return bool(llm_settings.get("openrouter_cache_control", False))


def _final_score_instruction(output_format: str | None) -> str:
    """Brief score-scale reminder matching the selected final prompt."""
    return (
        "score must be one of:\n"
        "- 1: The report explicitly states or clearly paraphrases the knowledge required by the rubric and covers the key elements\n"
        "- 0.5: The report covers some substantive key elements of the rubric, but misses other necessary elements or qualifiers\n"
        "- 0: The report does not mention the specific knowledge, is only generally related, or factually contradicts the rubric\n"
    )


def _normalize_final_result(
    obj: dict[str, Any],
    rubric_object: dict[str, Any],
    *,
    output_format: str = "score_only_half",
) -> dict[str, Any]:
    """Normalize final scorer output and extract the first rubric result."""
    rubric_results = obj.get("rubric_results")
    if isinstance(rubric_results, list) and rubric_results:
        first = rubric_results[0]
    elif isinstance(obj, dict):
        first = obj
    else:
        first = {}
    score = _normalize_score(first.get("score"))
    verdict = _normalize_verdict(str(first.get("verdict", "")).strip())
    if verdict == "not_satisfied" and score == 1:
        verdict = "satisfied"
    normalized = {
        "rubric_id": str(first.get("rubric_id", rubric_object.get("rubric_id", ""))).strip() or str(rubric_object.get("rubric_id", "")),
        "verdict": verdict,
        "score": score,
        "confidence": _normalize_confidence(first.get("confidence")),
        "evidence_quotes": _normalize_string_list(first.get("evidence_quotes")),
        "short_reason": str(first.get("short_reason", "")).strip(),
        "missing_elements": _normalize_string_list(first.get("missing_elements")),
    }
    if _requires_reason(output_format):
        reason = _normalize_reason(first.get("reason", first.get("short_reason", "")))
        normalized["reason"] = reason
        normalized["short_reason"] = reason
    return normalized


def _normalize_final_batch_result(
    obj: dict[str, Any],
    rubric_objects: list[dict[str, Any]],
    *,
    output_format: str = "score_only_half",
) -> dict[str, Any]:
    """Normalize batch final scorer output."""
    rubric_results_raw = obj.get("rubric_results")
    if not isinstance(rubric_results_raw, list):
        rubric_results_raw = []

    rubric_by_id = {
        str(rubric.get("rubric_id", "")).strip(): rubric
        for rubric in rubric_objects
        if str(rubric.get("rubric_id", "")).strip()
    }
    normalized_results: list[dict[str, Any]] = []
    for raw_item in rubric_results_raw:
        if not isinstance(raw_item, dict):
            continue
        rubric_id = str(raw_item.get("rubric_id", "")).strip()
        rubric_object = rubric_by_id.get(rubric_id)
        if rubric_object is None:
            continue
        normalized = _normalize_final_result(raw_item, rubric_object, output_format=output_format)
        normalized_results.append(normalized)

    satisfied_count = sum(1 for item in normalized_results if float(item.get("score", 0) or 0) >= 1.0)
    computed_overall = (
        sum(float(item.get("score", 0.0) or 0.0) for item in normalized_results) / len(normalized_results)
        if normalized_results
        else 0.0
    )
    major_missing_points = _normalize_string_list(obj.get("major_missing_points"))
    unsupported_claims = _normalize_string_list(obj.get("unsupported_claims"))
    final_diagnosis = str(obj.get("final_diagnosis", "")).strip()

    if not final_diagnosis:
        final_diagnosis = (
            f"Final scoring finished with coverage ratio {computed_overall:.4f}. "
            f"{satisfied_count}/{len(normalized_results)} rubrics were fully satisfied."
        )

    return {
        "rubric_results": normalized_results,
        "overall_score": computed_overall,
        "major_missing_points": major_missing_points,
        "unsupported_claims": unsupported_claims,
        "final_diagnosis": final_diagnosis,
    }


def _normalize_string_list(value: Any) -> list[str]:
    """Normalize a maybe-list into a clean list of strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_reason(value: Any) -> str:
    """Normalize whitespace while preserving the full reason."""
    return " ".join(str(value or "").split())


def _normalize_confidence(value: Any) -> float:
    """Clamp confidence to [0, 1]."""
    try:
        parsed = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, parsed))


def _normalize_score(value: Any) -> float | int:
    """Normalize a half-credit score after batch validation."""
    try:
        parsed = float(value)
    except Exception:
        return 0
    if parsed >= 0.75:
        return 1
    if parsed >= 0.25:
        return 0.5
    return 0


def _requires_reason(output_format: str | None) -> bool:
    """Whether half-credit results must include reasons."""
    return str(output_format or "").strip().lower() in {
        "score_reason_half", "score_reason_half_credit",
    }


def _normalize_verdict(value: str) -> str:
    """Normalize verdict strings."""
    normalized = value.strip().lower().replace(" ", "_")
    if normalized in {"satisfied", "not_satisfied"}:
        return normalized
    return "not_satisfied"


def _emit_attempt_event(
    attempt_hook: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
    **extra: Any,
) -> None:
    """Forward judge attempt events with scoring-specific context."""
    if attempt_hook is None:
        return
    payload = dict(event)
    payload.update(extra)
    attempt_hook(payload)


def _validate_final_batch_payload(
    obj: dict[str, Any],
    rubric_objects: list[dict[str, Any]],
    *,
    output_format: str = "score_only_half",
) -> None:
    """Require batch judge output to cover the requested rubric ids exactly once."""
    rubric_results_raw = obj.get("rubric_results")
    if not isinstance(rubric_results_raw, list):
        raise ValueError("batch judge output missing list field 'rubric_results'")

    expected_ids = [
        str(rubric.get("rubric_id", "")).strip()
        for rubric in rubric_objects
        if str(rubric.get("rubric_id", "")).strip()
    ]
    returned_ids: list[str] = []
    malformed_count = 0
    missing_reason_ids: list[str] = []
    for item in rubric_results_raw:
        if not isinstance(item, dict):
            malformed_count += 1
            continue
        if output_format == "score_only_half":
            score = item.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or score not in (0, 0.5, 1):
                raise ValueError("score_only_half requires an explicit numeric score in {0, 0.5, 1}")
        rubric_id = str(item.get("rubric_id", "")).strip()
        if rubric_id:
            returned_ids.append(rubric_id)
            if _requires_reason(output_format) and "reason" not in item:
                missing_reason_ids.append(rubric_id)
        else:
            malformed_count += 1

    expected_set = set(expected_ids)
    returned_set = set(returned_ids)
    missing_ids = [rubric_id for rubric_id in expected_ids if rubric_id not in returned_set]
    unexpected_ids = [rubric_id for rubric_id in returned_ids if rubric_id not in expected_set]

    seen_counts: dict[str, int] = {}
    duplicate_ids: list[str] = []
    for rubric_id in returned_ids:
        seen_counts[rubric_id] = seen_counts.get(rubric_id, 0) + 1
        if seen_counts[rubric_id] == 2:
            duplicate_ids.append(rubric_id)

    if malformed_count or missing_ids or unexpected_ids or duplicate_ids or missing_reason_ids or len(returned_ids) != len(expected_ids):
        preview = []
        if malformed_count:
            preview.append(f"malformed={malformed_count}")
        if missing_ids:
            preview.append(f"missing={missing_ids[:10]}")
        if unexpected_ids:
            preview.append(f"unexpected={unexpected_ids[:10]}")
        if duplicate_ids:
            preview.append(f"duplicates={duplicate_ids[:10]}")
        if missing_reason_ids:
            preview.append(f"missing_reason={missing_reason_ids[:10]}")
        preview.append(f"returned={len(returned_ids)}")
        preview.append(f"expected={len(expected_ids)}")
        raise ValueError("batch judge rubric id validation failed: " + ", ".join(preview))


def _invalidate_cache_path(cache_path: str | None) -> None:
    """Delete one cached LLM response after parse/validation failure."""
    path_text = str(cache_path or "").strip()
    if not path_text:
        return
    try:
        path = Path(path_text)
        if path.exists():
            path.unlink()
    except Exception:
        return


def _get_cached_client(llm_settings: dict[str, Any]) -> OpenAIChatClient:
    """Reuse OpenAI client instances for identical judge configs."""
    cfg = LLMConfig.from_settings(llm_settings)
    cache_key = (
        cfg.api_key,
        cfg.base_url,
        cfg.model,
        cfg.temperature,
        cfg.omit_temperature,
        cfg.timeout,
        cfg.max_retries,
        cfg.backoff_seconds,
        cfg.record_return_info,
        cfg.return_info_dir,
        cfg.run_tag,
        cfg.disable_env_proxy,
    )
    client = _CLIENT_CACHE.get(cache_key)
    if client is None:
        client = OpenAIChatClient(cfg)
        _CLIENT_CACHE[cache_key] = client
    return client
