"""LLM-based rubric generation from subgraph edge evidence inputs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from typing import Any, Callable

from drbenchpro.llm.client import LLMConfig, OpenAIChatClient, invalidate_cache_path, parse_first_json_object
from drbenchpro.common.resources import load_prompt_spec, render_user_prompt
from drbenchpro.common.io import utc_now_iso


PROMPT_FILE = "subgraph_edge_rubric_generation.md"
BATCH_PROMPT_FILE = "subgraph_edge_rubric_generation_batch.md"

_CLIENT_CACHE: dict[tuple[str, str, str, float, float, int, float, bool, str, str, bool], OpenAIChatClient] = {}


def generate_subgraph_edge_rubrics_llm(
    edge_inputs_payload: dict[str, Any],
    llm_settings: dict[str, Any],
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    resume_payload: dict[str, Any] | None = None,
    partial_hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    retry_fallback_rubrics: bool = False,
) -> dict[str, Any]:
    """Generate one rubric object per subgraph edge from evidence-rich inputs."""
    cfg = LLMConfig.from_settings(llm_settings)
    prompt_spec = load_prompt_spec(PROMPT_FILE)
    batch_prompt_spec = load_prompt_spec(BATCH_PROMPT_FILE)
    max_workers = max(1, int(llm_settings.get("concurrency", 4)))

    grouped_inputs = edge_inputs_payload.get("subgraph_edge_rubric_inputs", [])
    if not isinstance(grouped_inputs, list):
        raise ValueError("edge_inputs_payload must contain a list at 'subgraph_edge_rubric_inputs'")

    resume_payload = resume_payload or {}
    resume_by_rubric_id: dict[str, dict[str, Any]] = {}
    for item in (resume_payload.get("subgraph_rubrics", []) if isinstance(resume_payload.get("subgraph_rubrics"), list) else []):
        if not isinstance(item, dict):
            continue
        for rubric in item.get("rubrics", []) or []:
            if not isinstance(rubric, dict):
                continue
            rubric_id = str(rubric.get("rubric_id", "")).strip()
            if retry_fallback_rubrics and bool(rubric.get("llm_rubric_failed", False)):
                continue
            if rubric_id:
                resume_by_rubric_id[rubric_id] = _compact_rubric_output(rubric)

    flat_inputs: list[tuple[str, dict[str, Any]]] = []
    grouped_meta: dict[str, dict[str, Any]] = {}
    for item in grouped_inputs:
        if not isinstance(item, dict):
            continue
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        grouped_meta[subgraph_id] = {
            "subgraph_id": subgraph_id,
            "subgraph_type": str(item.get("subgraph_type", "")).strip(),
            "anchor_node": str(item.get("anchor_node", "")).strip(),
        }
        for edge_input in item.get("edge_rubric_inputs", []):
            if isinstance(edge_input, dict):
                rubric_id = str(edge_input.get("rubric_id", "")).strip()
                if rubric_id and rubric_id in resume_by_rubric_id:
                    continue
                flat_inputs.append((subgraph_id, edge_input))

    results_by_subgraph: dict[str, list[dict[str, Any]]] = {subgraph_id: [] for subgraph_id in grouped_meta}
    completed = 0
    for item in grouped_inputs:
        if not isinstance(item, dict):
            continue
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        for edge_input in item.get("edge_rubric_inputs", []):
            if not isinstance(edge_input, dict):
                continue
            rubric_id = str(edge_input.get("rubric_id", "")).strip()
            if rubric_id and rubric_id in resume_by_rubric_id:
                results_by_subgraph[subgraph_id].append(dict(resume_by_rubric_id[rubric_id]))
                completed += 1

    pending_total = len(flat_inputs)
    grand_total = completed + pending_total
    requested_chunk_size = int(llm_settings.get("chunk_size", 1) or 1)
    effective_chunk_size = resolve_rubric_chunk_size(requested_chunk_size, pending_total)
    pending_chunks = chunk_subgraph_edge_inputs(flat_inputs, effective_chunk_size)
    llm_completed = estimate_llm_call_count(completed, effective_chunk_size)
    llm_total = llm_completed + len(pending_chunks)

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(pending_chunks)))) as executor:
        futures = {
            executor.submit(
                _generate_edge_rubric_chunk,
                cfg,
                prompt_spec.system_prompt,
                batch_prompt_spec.system_prompt,
                chunk,
                attempt_hook,
                chunk_idx,
                len(pending_chunks),
                effective_chunk_size,
            ): (chunk_idx, chunk)
            for chunk_idx, chunk in enumerate(pending_chunks, start=1)
        }
        for future in as_completed(futures):
            chunk_idx, chunk = futures[future]
            try:
                chunk_results = future.result()
                status = "ok"
                detail = ""
            except Exception as exc:
                chunk_results = {}
                for subgraph_id, edge_input in chunk:
                    fallback = _fallback_rubric(edge_input)
                    fallback["llm_rubric_failed"] = True
                    chunk_results[str(edge_input.get("edge_id", "")).strip()] = fallback
                status = "error"
                detail = str(exc)
            llm_completed += 1
            chunk_edge_ids: list[str] = []
            for subgraph_id, edge_input in chunk:
                edge_id = str(edge_input.get("edge_id", "")).strip()
                rubric = chunk_results.get(edge_id)
                if rubric is None:
                    rubric = _fallback_rubric(edge_input)
                    rubric["llm_rubric_failed"] = True
                    status = "error"
                    if not detail:
                        detail = f"missing rubric result for edge_id={edge_id}"
                results_by_subgraph[subgraph_id].append(rubric)
                chunk_edge_ids.append(edge_id)
                completed += 1
                if progress_hook is not None:
                    progress_hook(
                        {
                            "status": status,
                            "completed": completed,
                            "total": grand_total,
                            "llm_completed": llm_completed,
                            "llm_total": llm_total,
                            "chunk_index": chunk_idx,
                            "chunk_total": len(pending_chunks),
                            "chunk_size_used": effective_chunk_size,
                            "subgraph_id": subgraph_id,
                            "edge_id": edge_id,
                            "rubric_id": str(rubric.get("rubric_id", "")).strip(),
                            "source_title": str(edge_input.get("source_title", "")).strip(),
                            "target_title": str(edge_input.get("target_title", "")).strip(),
                            "detail": detail,
                        }
                    )
            if partial_hook is not None and chunk:
                partial_hook(
                    _build_subgraph_rubric_payload(
                        edge_inputs_payload=edge_inputs_payload,
                        grouped_meta=grouped_meta,
                        results_by_subgraph=results_by_subgraph,
                        rubric_mode="llm",
                        edge_input_count=grand_total,
                        chunk_size_requested=requested_chunk_size,
                        chunk_size_used=effective_chunk_size,
                        llm_call_count=llm_completed,
                    ),
                    {
                        "phase": "rubrics",
                        "completed_rubrics": completed,
                        "total_rubrics": grand_total,
                        "llm_completed": llm_completed,
                        "llm_total": llm_total,
                        "chunk_index": chunk_idx,
                        "chunk_total": len(pending_chunks),
                        "edge_id": str(chunk[-1][1].get("edge_id", "")).strip(),
                        "chunk_edge_ids": chunk_edge_ids,
                    },
                )

    for subgraph_id, rubrics in results_by_subgraph.items():
        rubrics.sort(key=lambda item: str(item.get("rubric_id", "")))

    meta = edge_inputs_payload.get("metadata", {}) if isinstance(edge_inputs_payload.get("metadata", {}), dict) else {}
    return _build_subgraph_rubric_payload(
        edge_inputs_payload=edge_inputs_payload,
        grouped_meta=grouped_meta,
        results_by_subgraph=results_by_subgraph,
        rubric_mode="llm",
        edge_input_count=grand_total,
        chunk_size_requested=requested_chunk_size,
        chunk_size_used=effective_chunk_size,
        llm_call_count=llm_completed,
    )


def generate_subgraph_edge_rubrics_rule(
    edge_input_payload: dict[str, Any],
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    resume_payload: dict[str, Any] | None = None,
    partial_hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Generate deterministic rubric objects without an LLM."""
    grouped_inputs = edge_input_payload.get("subgraph_edge_rubric_inputs", [])
    if not isinstance(grouped_inputs, list):
        raise ValueError("edge_input_payload must contain a list at 'subgraph_edge_rubric_inputs'")

    meta = edge_input_payload.get("metadata", {}) if isinstance(edge_input_payload.get("metadata", {}), dict) else {}
    resume_payload = resume_payload or {}
    resume_by_rubric_id: dict[str, dict[str, Any]] = {}
    for item in (resume_payload.get("subgraph_rubrics", []) if isinstance(resume_payload.get("subgraph_rubrics"), list) else []):
        if not isinstance(item, dict):
            continue
        for rubric in item.get("rubrics", []) or []:
            if not isinstance(rubric, dict):
                continue
            rubric_id = str(rubric.get("rubric_id", "")).strip()
            if rubric_id:
                resume_by_rubric_id[rubric_id] = _compact_rubric_output(rubric)

    grouped_outputs: list[dict[str, Any]] = []
    edge_count = len(resume_by_rubric_id)
    total = sum(
        len(item.get("edge_rubric_inputs", []) or [])
        for item in grouped_inputs
        if isinstance(item, dict)
    )
    for item in grouped_inputs:
        if not isinstance(item, dict):
            continue
        rubrics = []
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        for edge_input in item.get("edge_rubric_inputs", []):
            if not isinstance(edge_input, dict):
                continue
            edge_id = str(edge_input.get("edge_id", "")).strip()
            rubric_id = str(edge_input.get("rubric_id", "")).strip()
            if rubric_id and rubric_id in resume_by_rubric_id:
                rubric = dict(resume_by_rubric_id[rubric_id])
            else:
                rubric = _fallback_rubric(edge_input)
                edge_count += 1
                if progress_hook is not None:
                    progress_hook(
                        {
                            "status": "ok",
                            "completed": edge_count,
                            "total": total,
                            "subgraph_id": subgraph_id,
                            "edge_id": edge_id,
                            "rubric_id": str(rubric.get("rubric_id", "")).strip(),
                            "source_title": str(edge_input.get("source_title", "")).strip(),
                            "target_title": str(edge_input.get("target_title", "")).strip(),
                            "detail": "",
                        }
                    )
                if partial_hook is not None:
                    partial_hook(
                        {
                            "metadata": {
                                "artifact_type": "subgraph_edge_rubrics",
                                "graph_id": str(meta.get("graph_id", "")).strip(),
                                "seed_entity": str(meta.get("seed_entity", "")).strip(),
                                "build_time_utc": utc_now_iso(),
                                "rubric_mode": "rule",
                                "edge_input_count": edge_count,
                                "subgraph_count": len(grouped_outputs) + 1,
                                "source_subgraphs_path": str(meta.get("source_subgraphs_path", "")).strip(),
                            },
                            "subgraph_rubrics": grouped_outputs + [
                                {
                                    "subgraph_id": subgraph_id,
                                    "subgraph_type": str(item.get("subgraph_type", "")).strip(),
                                    "anchor_node": str(item.get("anchor_node", "")).strip(),
                                    "rubrics": list(rubrics),
                                }
                            ],
                        },
                        {
                            "phase": "rubrics",
                            "completed_rubrics": edge_count,
                            "total_rubrics": total,
                            "edge_id": edge_id,
                        },
                    )
            rubrics.append(rubric)
        grouped_outputs.append(
            {
                "subgraph_id": subgraph_id,
                "subgraph_type": str(item.get("subgraph_type", "")).strip(),
                "anchor_node": str(item.get("anchor_node", "")).strip(),
                "rubrics": rubrics,
            }
        )

    return {
        "metadata": {
            "artifact_type": "subgraph_edge_rubrics",
            "graph_id": str(meta.get("graph_id", "")).strip(),
            "seed_entity": str(meta.get("seed_entity", "")).strip(),
            "build_time_utc": utc_now_iso(),
            "rubric_mode": "rule",
            "edge_input_count": edge_count,
            "subgraph_count": len(grouped_outputs),
            "source_subgraphs_path": str(meta.get("source_subgraphs_path", "")).strip(),
            "chunk_size_requested": 1,
            "chunk_size_used": 1,
            "llm_call_count": 0,
        },
        "subgraph_rubrics": grouped_outputs,
    }


def resolve_rubric_chunk_size(chunk_size: int, total_items: int) -> int:
    """Normalize requested chunk size; <=0 means put all pending edges into one chunk."""
    if total_items <= 0:
        return 1
    if int(chunk_size) <= 0:
        return total_items
    return max(1, int(chunk_size))


def estimate_llm_call_count(completed_rubrics: int, chunk_size: int) -> int:
    if completed_rubrics <= 0:
        return 0
    return int(ceil(int(completed_rubrics) / max(1, int(chunk_size))))


def chunk_subgraph_edge_inputs(
    flat_inputs: list[tuple[str, dict[str, Any]]],
    chunk_size: int,
) -> list[list[tuple[str, dict[str, Any]]]]:
    """Split pending edge inputs into stable chunks for one-call-many-rubrics generation."""
    if not flat_inputs:
        return []
    effective_size = len(flat_inputs) if int(chunk_size) <= 0 else max(1, int(chunk_size))
    return [
        flat_inputs[start : start + effective_size]
        for start in range(0, len(flat_inputs), effective_size)
    ]


def _build_subgraph_rubric_payload(
    *,
    edge_inputs_payload: dict[str, Any],
    grouped_meta: dict[str, dict[str, Any]],
    results_by_subgraph: dict[str, list[dict[str, Any]]],
    rubric_mode: str,
    edge_input_count: int,
    chunk_size_requested: int,
    chunk_size_used: int,
    llm_call_count: int,
) -> dict[str, Any]:
    meta = edge_inputs_payload.get("metadata", {}) if isinstance(edge_inputs_payload.get("metadata", {}), dict) else {}
    return {
        "metadata": {
            "artifact_type": "subgraph_edge_rubrics",
            "graph_id": str(meta.get("graph_id", "")).strip(),
            "seed_entity": str(meta.get("seed_entity", "")).strip(),
            "build_time_utc": utc_now_iso(),
            "rubric_mode": rubric_mode,
            "edge_input_count": edge_input_count,
            "subgraph_count": len(grouped_meta),
            "source_subgraphs_path": str(meta.get("source_subgraphs_path", "")).strip(),
            "chunk_size_requested": int(chunk_size_requested),
            "chunk_size_used": int(chunk_size_used),
            "llm_call_count": int(llm_call_count),
        },
        "subgraph_rubrics": [
            {
                **grouped_meta[subgraph_id],
                "rubrics": results_by_subgraph.get(subgraph_id, []),
            }
            for subgraph_id in grouped_meta
        ],
    }


def _generate_single_edge_rubric(
    cfg: LLMConfig,
    system_prompt: str,
    edge_input: dict[str, Any],
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    client = _get_cached_client_from_config(cfg)
    user_prompt = render_user_prompt(
        PROMPT_FILE,
        rubric_id=str(edge_input.get("rubric_id", "")).strip(),
        source_title=str(edge_input.get("source_title", "")).strip(),
        target_title=str(edge_input.get("target_title", "")).strip(),
        source_page_summary=str(edge_input.get("source_page_summary", "")).strip(),
        target_page_summary=str(edge_input.get("target_page_summary", "")).strip(),
        relation_evidence_sentence=str(edge_input.get("relation_evidence_sentence", "")).strip(),
        relation_evidence_paragraph=str(edge_input.get("relation_evidence_paragraph", "")).strip(),
        relation_evidence_source=str(edge_input.get("relation_evidence_source", "")).strip(),
        relation_evidence_anchor_text=str(edge_input.get("relation_evidence_anchor_text", "")).strip(),
        evidence_confidence=str(edge_input.get("evidence_confidence", "")).strip(),
    )
    source_title = str(edge_input.get("source_title", "")).strip()
    target_title = str(edge_input.get("target_title", "")).strip()
    edge_id = str(edge_input.get("edge_id", "")).strip()

    raw, meta_out = client.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        task_type="subgraph_edge_rubric",
        call_context={
            "edge_id": edge_id,
            "source_title": source_title,
            "target_title": target_title,
        },
        return_meta=True,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="subgraph_edge_rubric",
                edge_id=edge_id,
                source_title=source_title,
                target_title=target_title,
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
        invalidate_cache_path(cache_path)
        raise
    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)
    return _normalize_edge_rubric_object(obj, edge_input)


def _generate_edge_rubric_chunk(
    cfg: LLMConfig,
    single_system_prompt: str,
    batch_system_prompt: str,
    chunk: list[tuple[str, dict[str, Any]]],
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    chunk_index: int = 1,
    chunk_total: int = 1,
    chunk_size_used: int = 1,
) -> dict[str, dict[str, Any]]:
    if not chunk:
        return {}
    if len(chunk) == 1 and int(chunk_size_used) == 1:
        _subgraph_id, edge_input = chunk[0]
        edge_id = str(edge_input.get("edge_id", "")).strip()
        return {edge_id: _generate_single_edge_rubric(cfg, single_system_prompt, edge_input, attempt_hook)}

    client = _get_cached_client_from_config(cfg)
    raw_chunk_inputs = [edge_input for _, edge_input in chunk]
    chunk_inputs = [_compact_edge_input_for_rubric_prompt(edge_input) for edge_input in raw_chunk_inputs]
    edge_defaults = {
        str(edge_input.get("rubric_id", "")).strip(): edge_input
        for edge_input in raw_chunk_inputs
    }
    ordered_edge_ids = [str(edge_input.get("edge_id", "")).strip() for edge_input in raw_chunk_inputs]
    ordered_rubric_ids = [str(edge_input.get("rubric_id", "")).strip() for edge_input in chunk_inputs]
    user_prompt = render_user_prompt(BATCH_PROMPT_FILE, edge_rubric_inputs=chunk_inputs)
    raw, meta_out = client.chat(
        [
            {"role": "system", "content": batch_system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        task_type="subgraph_edge_rubric_batch",
        call_context={
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            "rubric_count": len(chunk_inputs),
            "chunk_edge_ids": ordered_edge_ids,
            "chunk_rubric_ids": ordered_rubric_ids,
        },
        return_meta=True,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="subgraph_edge_rubric_batch",
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                rubric_count=len(chunk_inputs),
                chunk_edge_ids=ordered_edge_ids,
                chunk_rubric_ids=ordered_rubric_ids,
            ))
            if attempt_hook is not None
            else None
        ),
    )
    record_path = str(meta_out.get("record_path", ""))
    cache_path = str(meta_out.get("cache_path", ""))
    try:
        obj = parse_first_json_object(raw)
        rubric_items = obj.get("rubrics", [])
        if not isinstance(rubric_items, list):
            raise ValueError("Batch rubric response missing list at 'rubrics'")
        results: dict[str, dict[str, Any]] = {}
        for item in rubric_items:
            if not isinstance(item, dict):
                continue
            rubric_id = str(item.get("rubric_id", "")).strip()
            if not rubric_id or rubric_id not in edge_defaults:
                continue
            edge_id = str(edge_defaults[rubric_id].get("edge_id", "")).strip()
            results[edge_id] = _normalize_edge_rubric_object(item, edge_defaults[rubric_id])
        missing_rubric_ids = [
            rubric_id
            for rubric_id, edge_id in zip(ordered_rubric_ids, ordered_edge_ids, strict=False)
            if edge_id not in results
        ]
        if missing_rubric_ids:
            raise ValueError(f"Batch rubric response missing rubric_ids: {', '.join(missing_rubric_ids)}")
    except Exception as exc:
        client.annotate_record(record_path=record_path, parsed_object=None, parse_error=str(exc))
        invalidate_cache_path(cache_path)
        raise
    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)
    return {edge_id: results[edge_id] for edge_id in ordered_edge_ids}


def _compact_edge_input_for_rubric_prompt(edge_input: dict[str, Any]) -> dict[str, Any]:
    """Keep LLM-facing rubric input free of graph ids/edge ids that are only internal join keys."""
    return {
        "rubric_id": str(edge_input.get("rubric_id", "")).strip(),
        "source_title": str(edge_input.get("source_title", "")).strip(),
        "target_title": str(edge_input.get("target_title", "")).strip(),
        "source_page_summary": str(edge_input.get("source_page_summary", "")).strip(),
        "target_page_summary": str(edge_input.get("target_page_summary", "")).strip(),
        "relation_evidence_sentence": str(edge_input.get("relation_evidence_sentence", "")).strip(),
        "relation_evidence_paragraph": str(edge_input.get("relation_evidence_paragraph", "")).strip(),
        "relation_evidence_source": str(edge_input.get("relation_evidence_source", "")).strip(),
        "relation_evidence_anchor_text": str(edge_input.get("relation_evidence_anchor_text", "")).strip(),
        "evidence_confidence": str(edge_input.get("evidence_confidence", "")).strip(),
    }


def _normalize_edge_rubric_object(obj: dict[str, Any], edge_input: dict[str, Any]) -> dict[str, Any]:
    rubric_text = _pick_first_text(
        obj.get("rubric_text"),
        obj.get("description"),
        obj.get("requirement"),
        obj.get("coverage_requirement"),
        obj.get("knowledge_requirement"),
    )
    return {
        "rubric_id": _pick_first_text(obj.get("rubric_id"), edge_input.get("rubric_id")) or str(edge_input.get("rubric_id", "")).strip(),
        "rubric_text": rubric_text,
        "rubric_depth": _rubric_depth_from_edge_input(edge_input),
        "llm_rubric_failed": False,
    }


def _fallback_rubric(edge_input: dict[str, Any]) -> dict[str, Any]:
    source_title = str(edge_input.get("source_title", "")).strip()
    target_title = str(edge_input.get("target_title", "")).strip()
    evidence_sentence = str(edge_input.get("relation_evidence_sentence", "")).strip()
    evidence_paragraph = str(edge_input.get("relation_evidence_paragraph", "")).strip()
    rubric_text = (
        f"The report explicitly states or clearly paraphrases the relationship between "
        f"'{source_title}' and '{target_title}' using Wikipedia-grounded evidence."
    )
    return {
        "rubric_id": str(edge_input.get("rubric_id", "")).strip(),
        "rubric_text": rubric_text,
        "rubric_depth": _rubric_depth_from_edge_input(edge_input),
        "llm_rubric_failed": False,
    }


def _compact_rubric_output(rubric: dict[str, Any]) -> dict[str, Any]:
    """Normalize persisted rubric records to the compact public artifact shape."""
    rubric_text = _pick_first_text(
        rubric.get("rubric_text"),
        rubric.get("description"),
        rubric.get("requirement"),
        rubric.get("coverage_requirement"),
        rubric.get("knowledge_requirement"),
    )
    return {
        "rubric_id": str(rubric.get("rubric_id", "")).strip(),
        "rubric_text": rubric_text,
        "rubric_depth": _rubric_depth_from_rubric(rubric),
        "llm_rubric_failed": bool(rubric.get("llm_rubric_failed", False)),
    }


def _rubric_depth_from_edge_input(edge_input: dict[str, Any]) -> int:
    """Return the rubric hop depth, defined as the target node depth of this edge."""
    return _to_int(edge_input.get("edge_depth"), default=0)


def _rubric_depth_from_rubric(rubric: dict[str, Any]) -> int:
    """Read depth from rubric_depth, edge_depth or target_depth, in that order."""
    for key in ("rubric_depth", "edge_depth", "target_depth"):
        value = rubric.get(key)
        if value is not None:
            return _to_int(value, default=0)
    return 0


def _extract_verification_hint(obj: dict[str, Any]) -> dict[str, Any]:
    raw = obj.get("verification_hint", {})
    if not isinstance(raw, dict):
        raw = {}
    should_present = raw.get("what_should_be_present", []) or obj.get("what_should_be_present", [])
    not_enough = raw.get("what_is_not_enough", []) or obj.get("what_is_not_enough", [])
    if isinstance(should_present, str):
        should_present = [should_present]
    if isinstance(not_enough, str):
        not_enough = [not_enough]
    if not isinstance(should_present, list):
        should_present = []
    if not isinstance(not_enough, list):
        not_enough = []
    return {
        "expected_coverage_type": _normalize_expected_coverage_type(
            _pick_first_text(raw.get("expected_coverage_type"), obj.get("expected_coverage_type"))
        ),
        "what_should_be_present": [str(item).strip() for item in should_present if str(item).strip()],
        "what_is_not_enough": [str(item).strip() for item in not_enough if str(item).strip()],
    }


def _normalize_expected_coverage_type(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"explicit_mention", "mention", "entity_mention"}:
        return "explicit_mention"
    if text in {"explicit_paraphrase", "paraphrase", "summary"}:
        return "explicit_paraphrase"
    return "explicit_relation"


def _pick_first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, dict)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _emit_attempt_event(
    attempt_hook: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
    **extra: Any,
) -> None:
    if attempt_hook is None:
        return
    payload = dict(event)
    payload.update(extra)
    attempt_hook(payload)


def _get_cached_client_from_config(cfg: LLMConfig) -> OpenAIChatClient:
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
