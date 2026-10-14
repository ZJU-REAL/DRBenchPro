"""Relation label generation for graph edges."""

from __future__ import annotations

import re
from typing import Any, Callable

from drbenchpro.llm.client import LLMConfig, OpenAIChatClient, invalidate_cache_path, parse_first_json_object
from drbenchpro.common.resources import load_prompt_spec, render_user_prompt

RELATION_PROMPT_FILE = "relation_generation.md"
BATCH_RELATION_PROMPT_FILE = "relation_generation_batch.md"


_CLIENT_CACHE: dict[tuple[str, str, str, float, float, int, float, bool, str, str, bool], OpenAIChatClient] = {}


def generate_relation(
    edge_evidence: dict[str, Any],
    mode: str = "none",
    llm_settings: dict[str, Any] | None = None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str | None, str | None]:
    """
    Generate relation label from edge evidence with pluggable mode.

    Modes:
    - none: do not assign relation label.
    - rule: conservative rule-based labeling.
    - llm: llm-based labeling with prompt-guided free-form relation strings.
    """
    mode = (mode or "none").lower()
    if mode == "none":
        return None, None
    if mode == "rule":
        return _rule_relation(edge_evidence), "rule heuristic"
    if mode == "llm":
        return _llm_relation(edge_evidence, llm_settings=llm_settings, attempt_hook=attempt_hook)
    raise ValueError(f"Unsupported relation mode: {mode}")


def generate_relations_batch(
    edge_evidences: list[dict[str, Any]],
    llm_settings: dict[str, Any] | None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, str]]:
    """Generate relation labels for a full edge set in one batch LLM call."""
    if not llm_settings:
        raise ValueError("llm_settings is required when relation mode is llm_batch.")

    client = _get_cached_client(llm_settings)
    prompt_spec = load_prompt_spec(BATCH_RELATION_PROMPT_FILE)
    compact_edges = []
    for edge in edge_evidences:
        compact_edges.append(
            {
                "edge_id": str(edge.get("edge_id", "")).strip(),
                "source_node": str(edge.get("source_title", "")).strip(),
                "target_node": str(edge.get("target_title", "")).strip(),
                "anchor_text": str(edge.get("anchor_text", "")).strip()[:120],
                "source_context": str(edge.get("context_text", ""))[:360],
                "target_snippet": str(edge.get("target_text_snippet", ""))[:220],
            }
        )

    user_prompt = render_user_prompt(BATCH_RELATION_PROMPT_FILE, edge_evidence_list=compact_edges)
    raw, meta = client.chat(
        [
            {"role": "system", "content": prompt_spec.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        task_type="relation_batch",
        call_context={"edge_count": len(compact_edges)},
        return_meta=True,
        attempt_hook=(
            (lambda event: _emit_attempt_event(attempt_hook, event, phase="relation_batch", edge_count=len(compact_edges)))
            if attempt_hook is not None
            else None
        ),
    )
    record_path = str(meta.get("record_path", ""))
    cache_path = str(meta.get("cache_path", ""))
    try:
        obj = parse_first_json_object(raw)
    except Exception as exc:
        client.annotate_record(record_path=record_path, parsed_object=None, parse_error=str(exc))
        invalidate_cache_path(cache_path)
        raise
    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)

    relation_items = obj.get("relations")
    if not isinstance(relation_items, list):
        relation_items = []
    results: dict[str, dict[str, str]] = {}
    for item in relation_items:
        if not isinstance(item, dict):
            continue
        edge_id = str(item.get("edge_id", "")).strip()
        if not edge_id:
            continue
        raw_label = str(item.get("candidate_relation", "")).strip().lower()
        label = _normalize_relation_label(raw_label) or "other"
        relation_definition = str(item.get("relation_definition", "")).strip()
        rationale = str(item.get("evidence_or_rationale", "")).strip()
        results[edge_id] = {
            "relation_label": label,
            "relation_explanation": relation_definition or rationale or "llm batch relation generation",
        }
    return results


def _rule_relation(edge: dict[str, Any]) -> str:
    """
    Conservative heuristic labeling inspired by update_relation.py's explicit relation stage.

    We avoid strong semantic claims and only output coarse categories.
    """
    target = str(edge.get("target_title", "")).lower()
    anchor = str(edge.get("anchor_text", "")).lower()
    context = str(edge.get("context_text", "")).lower()
    href = str(edge.get("href", "")).lower()
    evidence_text = " ".join([target, anchor, context, href])

    if "category:" in href:
        return "category"

    if _contains_any(
        evidence_text,
        [
            " award",
            " prize",
            " medal",
            " trophy",
            " academy award",
            " nobel",
            " grammy",
            " oscar",
            " pulitzer",
        ],
    ):
        return "award"

    if _contains_any(
        evidence_text,
        [
            " olympics",
            " election",
            " conference",
            " festival",
            " war",
            " battle",
            " season",
            " tournament",
            " championship",
            " cup",
            " event",
        ],
    ):
        return "event"

    if _contains_any(
        evidence_text,
        [
            " university",
            " institute",
            " company",
            " corporation",
            " agency",
            " association",
            " committee",
            " government",
            " museum",
            " foundation",
            " organization",
            " orchestra",
            " studio",
        ],
    ):
        return "organization"

    if _contains_any(
        evidence_text,
        [
            " film",
            " album",
            " song",
            " novel",
            " book",
            " opera",
            " painting",
            " series",
            " poem",
            " work",
            " documentary",
            " publication",
        ],
    ):
        return "work"

    if _contains_any(
        evidence_text,
        [
            " city",
            " country",
            " province",
            " state",
            " village",
            " district",
            " county",
            " river",
            " mountain",
            " lake",
            " island",
            " located in",
            " located at",
            " capital",
        ],
    ):
        return "place"

    return "other"


def _llm_relation(
    edge: dict[str, Any],
    llm_settings: dict[str, Any] | None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, str]:
    """Generate relation label with OpenAI-compatible LLM."""
    if not llm_settings:
        raise ValueError("llm_settings is required when relation mode is llm.")

    client = _get_cached_client(llm_settings)

    source = str(edge.get("source_title", ""))
    target = str(edge.get("target_title", ""))
    anchor = str(edge.get("anchor_text", ""))
    context = str(edge.get("context_text", ""))[:2200]
    target_snippet = str(edge.get("target_text_snippet", ""))[:1200]
    source_neighbors = edge.get("source_neighbors", [])
    target_neighbors = edge.get("target_neighbors", [])
    existing_relation_hint = str(edge.get("relation_hint", "")).strip()

    prompt_spec = load_prompt_spec(RELATION_PROMPT_FILE)
    user_prompt = render_user_prompt(
        RELATION_PROMPT_FILE,
        source_node=source,
        target_node=target,
        anchor_text=anchor,
        source_context=context,
        target_snippet=target_snippet,
        source_neighbors=source_neighbors or [],
        target_neighbors=target_neighbors or [],
        existing_relation_hint=existing_relation_hint or "",
    )

    raw, meta = client.chat(
        [
            {"role": "system", "content": prompt_spec.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        task_type="relation",
        call_context={
            "source_title": source,
            "target_title": target,
            "anchor_text": anchor,
        },
        return_meta=True,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="relation",
                source_title=source,
                target_title=target,
                anchor_text=anchor,
            ))
            if attempt_hook is not None
            else None
        ),
    )
    record_path = str(meta.get("record_path", ""))
    cache_path = str(meta.get("cache_path", ""))
    try:
        obj = parse_first_json_object(raw)
    except Exception as exc:
        client.annotate_record(record_path=record_path, parsed_object=None, parse_error=str(exc))
        invalidate_cache_path(cache_path)
        raise
    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)
    raw_label = str(obj.get("candidate_relation", "")).strip().lower()
    label = _normalize_relation_label(raw_label)
    if not label:
        label = "other"
    relation_definition = str(obj.get("relation_definition", "")).strip()
    rationale = str(obj.get("evidence_or_rationale", "")).strip()
    explanation = relation_definition or rationale or "llm relation generation"
    return label, explanation


def _get_cached_client(llm_settings: dict[str, Any]) -> OpenAIChatClient:
    """Reuse OpenAI client instances for identical configs."""
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


def _contains_any(text: str, keywords: list[str]) -> bool:
    """Case-insensitive keyword contains check."""
    return any(keyword in text for keyword in keywords)


def _normalize_relation_label(label: str) -> str:
    """Normalize candidate relation into a program-friendly snake_case label."""
    if not label:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return normalized[:80]


def _emit_attempt_event(
    attempt_hook: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
    **extra: Any,
) -> None:
    """Forward LLM attempt events with relation-generation context."""
    if attempt_hook is None:
        return
    payload = dict(event)
    payload.update(extra)
    attempt_hook(payload)
