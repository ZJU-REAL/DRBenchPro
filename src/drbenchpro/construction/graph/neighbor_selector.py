"""LLM-assisted neighbor selection for Wikipedia graph expansion."""

from __future__ import annotations

import re
from typing import Any, Callable

from drbenchpro.construction.graph.schemas import AnchorEvidence, EntityNode
from drbenchpro.llm.client import LLMConfig, OpenAIChatClient, invalidate_cache_path, parse_first_json_object
from drbenchpro.common.resources import load_prompt_spec, render_user_prompt
from drbenchpro.common.text import safe_snippet


PROMPT_FILE = "graph_neighbor_selection.md"
ROOT_FULL_PAGE_PROMPT_FILE = "graph_root_full_page_selection.md"

_CLIENT_CACHE: dict[tuple[str, str, str, float, float, int, float, bool, str, str, bool], OpenAIChatClient] = {}


def select_neighbors_llm(
    source_entity: EntityNode,
    candidate_anchors: list[AnchorEvidence],
    limit: int,
    llm_settings: dict[str, Any],
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
) -> list[AnchorEvidence]:
    """Select the top neighbor anchors for one node using an LLM."""
    if limit <= 0 or not candidate_anchors:
        return []
    if not llm_settings:
        raise ValueError("llm_settings is required when neighbor_selection_mode=llm.")

    client = _get_cached_client(llm_settings)
    prompt_spec = load_prompt_spec(PROMPT_FILE)

    source_summary = " ".join((source_entity.text or [])[:2]).strip()
    source_summary = safe_snippet(source_summary or source_entity.text_snippet or "", max_chars=1600)
    intro_paragraph_count = max(0, len(source_entity.text or []))

    candidate_items: list[dict[str, Any]] = []
    anchor_by_candidate_id: dict[str, AnchorEvidence] = {}
    for idx, anchor in enumerate(candidate_anchors, start=1):
        candidate_id = f"cand_{idx:03d}"
        section = "lead" if intro_paragraph_count > 0 and int(anchor.paragraph_id) < intro_paragraph_count else "body"
        anchor_by_candidate_id[candidate_id] = anchor
        candidate_items.append(
            {
                "candidate_id": candidate_id,
                "wikipedia_title": str(anchor.wikipedia_title).strip(),
                "anchor_text": safe_snippet(str(anchor.text).strip(), max_chars=140),
                "section": section,
                "paragraph_id": int(anchor.paragraph_id),
                "context_text": safe_snippet(str(anchor.context_text).strip(), max_chars=260),
            }
        )

    user_prompt = render_user_prompt(
        PROMPT_FILE,
        source_title=str(source_entity.wikipedia_title).strip(),
        source_depth=int(source_entity.depth),
        source_summary=source_summary,
        target_neighbor_count=int(limit),
        candidate_neighbors=candidate_items,
    )

    raw, meta = client.chat(
        [
            {"role": "system", "content": prompt_spec.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        task_type="graph_neighbor_selection",
        call_context={
            "source_title": str(source_entity.wikipedia_title).strip(),
            "source_depth": int(source_entity.depth),
            "candidate_count": len(candidate_items),
            "target_count": int(limit),
        },
        return_meta=True,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="graph_neighbor_selection",
                source_title=str(source_entity.wikipedia_title).strip(),
                source_depth=int(source_entity.depth),
                candidate_count=len(candidate_items),
                target_count=int(limit),
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

    selected: list[AnchorEvidence] = []
    seen_titles: set[str] = set()

    for candidate_id in _extract_string_list(obj.get("selected_candidate_ids")):
        anchor = anchor_by_candidate_id.get(candidate_id)
        if anchor is None:
            continue
        title = str(anchor.wikipedia_title).strip()
        if not title or title in seen_titles:
            continue
        selected.append(anchor)
        seen_titles.add(title)
        if len(selected) >= limit:
            return selected

    for anchor in candidate_anchors:
        title = str(anchor.wikipedia_title).strip()
        if not title or title in seen_titles:
            continue
        selected.append(anchor)
        seen_titles.add(title)
        if len(selected) >= limit:
            break
    return selected


def select_root_full_page_neighbors_llm(
    source_entity: EntityNode,
    candidate_anchors: list[AnchorEvidence],
    lead_intro_anchors: list[AnchorEvidence],
    limit: int,
    llm_settings: dict[str, Any],
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    prompt_file: str | None = None,
) -> list[AnchorEvidence]:
    """Select root first-hop neighbors from full-page candidates."""
    if limit <= 0 or not candidate_anchors:
        return []
    if not llm_settings:
        raise ValueError("llm_settings is required when root_link_selection_mode=full_page_llm.")

    client = _get_cached_client(llm_settings)
    prompt_spec = load_prompt_spec(str(prompt_file or ROOT_FULL_PAGE_PROMPT_FILE).strip() or ROOT_FULL_PAGE_PROMPT_FILE)

    source_summary = " ".join((source_entity.text or [])[:3]).strip()
    source_summary = safe_snippet(source_summary or source_entity.text_snippet or "", max_chars=1800)

    lead_titles = {
        str(anchor.wikipedia_title).strip()
        for anchor in lead_intro_anchors
        if str(anchor.wikipedia_title).strip()
    }
    candidate_items: list[dict[str, Any]] = []
    anchor_by_candidate_id: dict[str, AnchorEvidence] = {}
    anchor_by_title: dict[str, AnchorEvidence] = {}
    for idx, anchor in enumerate(candidate_anchors, start=1):
        candidate_id = f"cand_{idx:03d}"
        title = str(anchor.wikipedia_title).strip()
        is_lead = bool(title and title in lead_titles)
        anchor_by_candidate_id[candidate_id] = anchor
        anchor_by_title[title] = anchor
        candidate_items.append(
            {
                "candidate_id": candidate_id,
                "wikipedia_title": title,
                "anchor_text": safe_snippet(str(anchor.text).strip(), max_chars=120),
                "section": "lead" if is_lead else "body",
                "paragraph_id": int(anchor.paragraph_id),
                "context_text": safe_snippet(str(anchor.context_text).strip(), max_chars=220),
            }
        )

    user_prompt = render_user_prompt(
        str(prompt_file or ROOT_FULL_PAGE_PROMPT_FILE).strip() or ROOT_FULL_PAGE_PROMPT_FILE,
        root_title=str(source_entity.wikipedia_title).strip(),
        root_summary=source_summary,
        target_neighbor_count=int(limit),
        candidate_neighbors=candidate_items,
    )

    raw, meta = client.chat(
        [
            {"role": "system", "content": prompt_spec.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        task_type="graph_root_full_page_selection",
        call_context={
            "source_title": str(source_entity.wikipedia_title).strip(),
            "source_depth": int(source_entity.depth),
            "candidate_count": len(candidate_items),
            "lead_intro_count": len(lead_titles),
            "target_count": int(limit),
        },
        return_meta=True,
        attempt_hook=(
            (lambda event: _emit_attempt_event(
                attempt_hook,
                event,
                phase="graph_root_full_page_selection",
                source_title=str(source_entity.wikipedia_title).strip(),
                source_depth=int(source_entity.depth),
                candidate_count=len(candidate_items),
                target_count=int(limit),
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

    selected_neighbor_ids, relation_details = _extract_root_selection_details(
        obj,
        anchor_by_candidate_id=anchor_by_candidate_id,
        anchor_by_title=anchor_by_title,
    )
    for candidate_id, details in relation_details.items():
        anchor = anchor_by_candidate_id.get(candidate_id)
        if anchor is not None:
            _annotate_root_selection_anchor(
                anchor,
                root_title=str(source_entity.wikipedia_title).strip(),
                relation_explanation=details.get("relation_explanation", ""),
                source="llm",
            )

    selected: list[AnchorEvidence] = []
    seen_titles: set[str] = set()
    seen_token_sets: list[set[str]] = []

    def add_anchor(anchor: AnchorEvidence) -> None:
        title = str(anchor.wikipedia_title).strip()
        if not title or title in seen_titles:
            return
        token_set = _title_similarity_tokens(title)
        if _is_near_duplicate_title(token_set, seen_token_sets):
            return
        _ensure_root_selection_anchor_annotation(anchor, root_title=str(source_entity.wikipedia_title).strip())
        selected.append(anchor)
        seen_titles.add(title)
        seen_token_sets.append(token_set)

    target_size = int(limit)
    ordered_candidate_ids = _extract_string_list(obj.get("selected_candidate_ids"))
    if not ordered_candidate_ids:
        ordered_candidate_ids = selected_neighbor_ids

    for candidate_id in ordered_candidate_ids:
        anchor = anchor_by_candidate_id.get(candidate_id)
        if anchor is None:
            continue
        add_anchor(anchor)
        if len(selected) >= target_size:
            return selected

    for anchor in candidate_anchors:
        add_anchor(anchor)
        if len(selected) >= target_size:
            break
    return selected


def _title_similarity_tokens(title: str) -> set[str]:
    """Return lightweight title tokens for near-duplicate root branch filtering."""
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(title).lower())
        if len(token) > 2 and token not in stopwords
    }


def _is_near_duplicate_title(candidate_tokens: set[str], seen_token_sets: list[set[str]]) -> bool:
    if not candidate_tokens:
        return False
    for existing_tokens in seen_token_sets:
        if not existing_tokens:
            continue
        overlap = len(candidate_tokens & existing_tokens)
        smaller = min(len(candidate_tokens), len(existing_tokens))
        larger = max(len(candidate_tokens), len(existing_tokens))
        if smaller <= 0:
            continue
        containment = overlap / smaller
        jaccard = overlap / larger
        if containment >= 0.8 or jaccard >= 0.72:
            return True
    return False


def _candidate_id_from_selection_reference(
    value: Any,
    *,
    anchor_by_candidate_id: dict[str, AnchorEvidence],
    anchor_by_title: dict[str, AnchorEvidence],
) -> str:
    if isinstance(value, dict):
        for key in ("candidate_id", "id", "selected_candidate_id"):
            candidate_id = str(value.get(key, "")).strip()
            if candidate_id in anchor_by_candidate_id:
                return candidate_id
        for key in ("wikipedia_title", "title", "selected_title"):
            title = str(value.get(key, "")).strip()
            if title and title in anchor_by_title:
                for candidate_id, anchor in anchor_by_candidate_id.items():
                    if str(anchor.wikipedia_title).strip() == title:
                        return candidate_id
        return ""

    text = str(value).strip()
    if text in anchor_by_candidate_id:
        return text
    if text in anchor_by_title:
        for candidate_id, anchor in anchor_by_candidate_id.items():
            if str(anchor.wikipedia_title).strip() == text:
                return candidate_id
    return ""


def _root_selection_text(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in keys:
            text = str(value.get(key, "")).strip()
            if text:
                return text
    return ""


def _extract_root_selection_details(
    obj: dict[str, Any],
    *,
    anchor_by_candidate_id: dict[str, AnchorEvidence],
    anchor_by_title: dict[str, AnchorEvidence],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    ordered_ids: list[str] = []
    details: dict[str, dict[str, str]] = {}
    containers: list[Any] = []
    for key in (
        "selected_neighbors",
        "selected_candidates",
        "selected_branches",
        "neighbor_relations",
        "candidate_relations",
    ):
        value = obj.get(key)
        if value:
            containers.append(value)

    for container in containers:
        if isinstance(container, dict):
            items = list(container.items())
        elif isinstance(container, list):
            items = [(None, item) for item in container]
        else:
            continue
        for reference, value in items:
            candidate_id = _candidate_id_from_selection_reference(
                value if reference is None else reference,
                anchor_by_candidate_id=anchor_by_candidate_id,
                anchor_by_title=anchor_by_title,
            )
            if not candidate_id and isinstance(value, dict):
                candidate_id = _candidate_id_from_selection_reference(
                    value,
                    anchor_by_candidate_id=anchor_by_candidate_id,
                    anchor_by_title=anchor_by_title,
                )
            if not candidate_id:
                continue
            if candidate_id not in ordered_ids:
                ordered_ids.append(candidate_id)
            row = details.setdefault(candidate_id, {})
            relation_explanation = _root_selection_text(
                value,
                (
                    "relation_explanation",
                    "root_relation",
                    "relationship_to_root",
                    "relation_to_root",
                    "explanation",
                ),
            )
            if relation_explanation:
                row["relation_explanation"] = relation_explanation
    return ordered_ids, details


def _fallback_root_relation_explanation(anchor: AnchorEvidence, root_title: str) -> str:
    title = str(anchor.wikipedia_title).strip()
    anchor_text = str(anchor.text).strip()
    context = safe_snippet(str(anchor.context_text).strip(), max_chars=260)
    if context:
        return (
            f"The root article '{root_title}' links to '{title}' via anchor text "
            f"'{anchor_text or title}' in this context: {context}"
        )
    return f"The root article '{root_title}' directly links to '{title}' as a selected first-hop expansion entity."


def _annotate_root_selection_anchor(
    anchor: AnchorEvidence,
    *,
    root_title: str,
    relation_explanation: str = "",
    source: str = "fallback",
) -> None:
    explanation = str(relation_explanation).strip() or _fallback_root_relation_explanation(anchor, root_title)
    setattr(anchor, "root_relation_explanation", explanation)
    setattr(anchor, "root_relation_explanation_source", source)


def _ensure_root_selection_anchor_annotation(anchor: AnchorEvidence, *, root_title: str) -> None:
    if str(getattr(anchor, "root_relation_explanation", "") or "").strip():
        return
    _annotate_root_selection_anchor(anchor, root_title=root_title, source="fallback")


def _extract_string_list(value: Any) -> list[str]:
    """Flatten a flexible prompt output into a plain string list."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        results: list[str] = []
        for item in value:
            results.extend(_extract_string_list(item))
        return results
    return []


def _get_cached_client(llm_settings: dict[str, Any]) -> OpenAIChatClient:
    """Reuse OpenAI clients for identical configs."""
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


def _emit_attempt_event(
    attempt_hook: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
    **extra: Any,
) -> None:
    """Forward llm attempt events with graph-neighbor-selection context."""
    if attempt_hook is None:
        return
    payload = dict(event)
    payload.update(extra)
    attempt_hook(payload)
