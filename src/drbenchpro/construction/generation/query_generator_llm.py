"""LLM-based research query generation from local graph evidence."""

from __future__ import annotations

import re
from typing import Any, Callable

from drbenchpro.llm.client import LLMConfig, OpenAIChatClient, invalidate_cache_path, parse_first_json_object
from drbenchpro.common.resources import load_prompt_spec, render_user_prompt
from drbenchpro.common.text import safe_snippet

QUERY_PROMPT_FILE = "query_generation_anti_leakage.md"
QUERY_GEN_MODES = ("less",)
MAX_FORBIDDEN_ENTITY_RETRIES = 3


def normalize_query_prompt_file(value: str | None = None) -> str:
    """Normalize a query prompt filename under the prompts/ directory."""
    filename = str(value or QUERY_PROMPT_FILE).strip() or QUERY_PROMPT_FILE
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Query prompt file must be a filename under prompts/: {value}")
    if not filename.endswith(".md"):
        filename = f"{filename}.md"
    return filename


def normalize_query_gen_mode(value: str) -> str:
    mode = str(value or "less").strip().lower()
    if mode not in set(QUERY_GEN_MODES):
        raise ValueError(f"Unsupported query_gen_mode: {value}")
    return mode


def resolve_query_prompt_file_for_mode(value: str | None = None, *, query_gen_mode: str = "less") -> str:
    """Resolve the effective query prompt file for a query-generation mode."""
    normalize_query_gen_mode(query_gen_mode)
    return normalize_query_prompt_file(value)


def generate_query_llm(
    graph: dict[str, Any],
    llm_settings: dict[str, Any],
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    query_gen_mode: str = "less",
    query_prompt_file: str | None = None,
) -> dict[str, Any]:
    """Generate one research-style query object via LLM with structured JSON output."""
    cfg = LLMConfig.from_settings(llm_settings)
    client = OpenAIChatClient(cfg)

    query_gen_mode = normalize_query_gen_mode(query_gen_mode)
    prompt_filename = resolve_query_prompt_file_for_mode(query_prompt_file, query_gen_mode=query_gen_mode)
    meta = graph.get("metadata", {})
    seed = meta.get("seed_entity", "")
    entities = graph.get("entities", [])
    edges = graph.get("edges", [])
    prompt_spec = load_prompt_spec(prompt_filename)
    summary = _build_query_graph_summary(graph, query_gen_mode=query_gen_mode)
    user_prompt = render_user_prompt(
        prompt_filename,
        query_gen_mode=query_gen_mode,
        graph_metadata={
            "graph_id": str(meta.get("graph_id", "")),
            "seed_entity": seed,
            "entity_count": len(entities),
            "edge_count": len(edges),
            "subgraph_id": str(meta.get("subgraph_id", "")),
            "subgraph_type": str(meta.get("subgraph_type", "")),
            "relation_mode": str(meta.get("relation_mode", "")),
        },
        seed_entity=seed,
        subgraph_overview=summary["subgraph_overview"],
        root_node=summary["root_node"],
        subgraph_nodes=summary["subgraph_nodes"],
        depth_1_branch_inputs=summary["depth_1_branch_inputs"],
        forbidden_entity_names=summary["forbidden_entity_names"],
    )
    messages = [
        {"role": "system", "content": prompt_spec.system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    call_context = {
        "seed_entity": seed,
        "graph_id": str(meta.get("graph_id", "")),
        "entity_count": len(entities),
        "edge_count": len(edges),
        "query_gen_mode": query_gen_mode,
        "query_prompt_file": prompt_filename,
    }
    emit_attempt_hook = (
        (lambda event: _emit_attempt_event(
            attempt_hook,
            event,
            phase="query",
            seed_entity=seed,
            graph_id=str(meta.get("graph_id", "")),
        ))
        if attempt_hook is not None
        else None
    )

    retried_after_bad_cache = False
    retried_after_invalid_response = False
    forbidden_retry_count = 0
    accepted_forbidden_mentions: list[str] = []
    while True:
        raw, meta_out = client.chat(
            messages,
            task_type="query",
            call_context=call_context,
            return_meta=True,
            attempt_hook=emit_attempt_hook,
        )
        record_path = str(meta_out.get("record_path", ""))
        cache_path = str(meta_out.get("cache_path", ""))
        cache_hit = bool(meta_out.get("cache_hit", False))

        obj: dict[str, Any] | None = None
        try:
            obj = parse_first_json_object(raw)
            normalized = _normalize_query_object(obj)
            query = str(normalized.get("query", "")).strip()
            if not query:
                raise ValueError("LLM query generation returned empty 'query'.")
            forbidden_mentions = _find_forbidden_entity_mentions(
                normalized,
                summary["forbidden_entity_names"],
                allowed_names={str(seed).strip()},
            )
            if forbidden_mentions:
                preview = ", ".join(forbidden_mentions[:8])
                error_message = f"LLM query generation leaked forbidden depth-1 entity names: {preview}"
                if forbidden_retry_count < MAX_FORBIDDEN_ENTITY_RETRIES:
                    forbidden_retry_count += 1
                    client.annotate_record(
                        record_path=record_path,
                        parsed_object=obj,
                        parse_error=f"{error_message}; retrying {forbidden_retry_count}/{MAX_FORBIDDEN_ENTITY_RETRIES}",
                    )
                    invalidate_cache_path(cache_path)
                    continue
                accepted_forbidden_mentions = list(forbidden_mentions)
        except Exception as exc:
            client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=str(exc))
            invalidate_cache_path(cache_path)
            if cache_hit and not retried_after_bad_cache:
                retried_after_bad_cache = True
                continue
            if not retried_after_invalid_response:
                retried_after_invalid_response = True
                continue
            raise

        client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)
        normalized["query_gen_mode"] = query_gen_mode
        normalized["query_prompt_file"] = prompt_filename
        if accepted_forbidden_mentions:
            normalized["forbidden_entity_policy"] = {
                "accepted_after_retry_exhausted": True,
                "max_forbidden_entity_retries": MAX_FORBIDDEN_ENTITY_RETRIES,
                "forbidden_mentions": accepted_forbidden_mentions,
            }
        normalized["query_generation_input_summary"] = {
            "subgraph_overview": summary["subgraph_overview"],
            "root_node": summary["root_node"],
            "subgraph_node_count": len(summary["subgraph_nodes"]),
            "depth_1_branch_count": len(summary["depth_1_branch_inputs"]),
            "forbidden_entity_count": len(summary["forbidden_entity_names"]),
            "forbidden_entity_scope": summary["forbidden_entity_scope"],
            "accepted_after_forbidden_retry_exhausted": bool(accepted_forbidden_mentions),
            "query_prompt_file": prompt_filename,
        }
        return normalized


def _emit_attempt_event(
    attempt_hook: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
    **extra: Any,
) -> None:
    """Forward LLM attempt events with query-generation context."""
    if attempt_hook is None:
        return
    payload = dict(event)
    payload.update(extra)
    attempt_hook(payload)


def _normalize_query_object(obj: dict[str, Any]) -> dict[str, Any]:
    """Normalize prompt-output variants into the compact query-only payload schema."""
    return {
        "query": _pick_first_text(
            obj.get("query"),
            obj.get("research_query"),
            obj.get("task_query"),
            obj.get("question"),
            obj.get("final_query"),
        )
    }


def _pick_first_text(*values: Any) -> str:
    """Return the first non-empty scalar text representation."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, dict)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _flatten_strings(value: Any) -> list[str]:
    """Flatten nested list/dict prompt outputs into a plain string list."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_flatten_strings(item))
        return items
    if isinstance(value, dict):
        items: list[str] = []
        for item in value.values():
            items.extend(_flatten_strings(item))
        return items
    text = str(value).strip()
    return [text] if text else []


_normalize_query_gen_mode = normalize_query_gen_mode


def _build_query_graph_summary(
    graph: dict[str, Any],
    *,
    query_gen_mode: str,
) -> dict[str, Any]:
    """Build the non-leaking query prompt payload for one sampled subgraph."""
    meta = graph.get("metadata", {})
    seed = str(meta.get("seed_entity", "")).strip()
    entities = graph.get("entities", [])
    edges = graph.get("edges", [])

    entities_by_title = {
        str(ent.get("wikipedia_title", "")).strip(): ent
        for ent in entities
        if isinstance(ent, dict) and str(ent.get("wikipedia_title", "")).strip()
    }
    root = entities_by_title.get(seed) or _first_entity_at_depth(entities, 0) or {}
    root_node = {
        "node_id": str(root.get("node_id") or root.get("entity_id") or root.get("_id") or "root").strip() or "root",
        "wikipedia_title": seed or str(root.get("wikipedia_title", "")).strip(),
        "summary": safe_snippet(str(root.get("text_snippet", "")), max_chars=700),
    }
    subgraph_nodes = _compact_subgraph_nodes_for_query_prompt(entities, seed_entity=seed)

    depth1_titles = _metadata_string_list(meta.get("depth_1_node_titles"))
    if not depth1_titles:
        depth1_titles = [
            str(ent.get("wikipedia_title", "")).strip()
            for ent in entities
            if isinstance(ent, dict) and int(ent.get("root_depth", -1) or -1) == 1
        ]
    if not depth1_titles:
        depth1_titles = _root_edge_target_titles(edges, seed)

    root_edges_by_target = _root_edges_by_target_title(edges, seed)
    depth_1_branch_inputs_rich: list[dict[str, Any]] = []
    for idx, title in enumerate(depth1_titles, start=1):
        if not title:
            continue
        entity = entities_by_title.get(title, {})
        edge = root_edges_by_target.get(title, {})
        relation_explanation = str(edge.get("relation_explanation", "")).strip()
        depth_1_branch_inputs_rich.append(
            {
                "branch_index": idx,
                "wikipedia_title": title,
                "summary": safe_snippet(str(entity.get("text_snippet", "")), max_chars=320),
                "root_relation_explanation": safe_snippet(relation_explanation, max_chars=360),
            }
        )
    depth_1_branch_inputs = depth_1_branch_inputs_rich

    depths = [
        int(ent.get("root_depth", 0) or 0)
        for ent in entities
        if isinstance(ent, dict)
    ]
    hard_forbidden_titles = list(depth1_titles)
    forbidden_entity_scope = "depth_1_branch_titles"
    forbidden_names_set: set[str] = set()
    for title in hard_forbidden_titles:
        if not title or title == seed:
            continue
        forbidden_names_set.update(_entity_name_variants(title))
    forbidden_entity_names = sorted(forbidden_names_set)
    subgraph_overview = {
        "node_count": len(entities),
        "edge_count": len(edges),
        "depth_1_branch_count": len(depth_1_branch_inputs),
        "max_depth": max(depths, default=0),
        "query_gen_mode": query_gen_mode,
        "instruction": _query_summary_instruction(query_gen_mode),
    }
    return {
        "subgraph_overview": subgraph_overview,
        "root_node": root_node,
        "subgraph_nodes": subgraph_nodes,
        "depth_1_branch_inputs": depth_1_branch_inputs,
        "forbidden_entity_names": forbidden_entity_names,
        "forbidden_entity_scope": forbidden_entity_scope,
    }


def _first_entity_at_depth(entities: list[Any], depth: int) -> dict[str, Any] | None:
    for ent in entities:
        if isinstance(ent, dict) and int(ent.get("root_depth", -1) or -1) == int(depth):
            return ent
    return None


def _compact_subgraph_nodes_for_query_prompt(entities: list[Any], *, seed_entity: str) -> list[dict[str, Any]]:
    """Return all sampled subgraph nodes as compact structural context for query generation."""
    rows: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for idx, ent in enumerate(entities, start=1):
        if not isinstance(ent, dict):
            continue
        title = str(ent.get("wikipedia_title", "")).strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        raw_depth = ent.get("root_depth", 0 if title == seed_entity else None)
        try:
            depth: int | None = int(raw_depth) if raw_depth is not None and str(raw_depth).strip() != "" else None
        except Exception:
            depth = None
        node_id = (
            str(ent.get("node_id", "")).strip()
            or str(ent.get("entity_id", "")).strip()
            or str(ent.get("_id", "")).strip()
            or f"node_{idx:04d}"
        )
        rows.append(
            {
                "node_id": node_id,
                "wikipedia_title": title,
                "root_depth": depth,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            10**9 if row.get("root_depth") is None else int(row.get("root_depth") or 0),
            str(row.get("wikipedia_title", "")),
            str(row.get("node_id", "")),
        ),
    )


def _metadata_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _root_edge_target_titles(edges: list[Any], seed: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source_title", "")).strip()
        target = str(edge.get("target_title", "")).strip()
        edge_depth = edge.get("edge_depth")
        if source == seed and target and target not in seen and (edge_depth in {None, "", 1} or str(edge_depth) == "1"):
            result.append(target)
            seen.add(target)
    return result


def _root_edges_by_target_title(edges: list[Any], seed: str) -> dict[str, dict[str, Any]]:
    by_target: dict[str, dict[str, Any]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source_title", "")).strip()
        target = str(edge.get("target_title", "")).strip()
        if source == seed and target:
            by_target.setdefault(target, edge)
    return by_target


def _query_summary_instruction(query_gen_mode: str) -> str:
    return (
        "Use sampled subgraph nodes and branch signals as internal construction evidence only. "
        "Do not reveal sampled non-root entity names or branch-specific clues in the final query."
    )


def _entity_name_variants(title: str) -> set[str]:
    clean = " ".join(str(title).strip().split())
    if not clean:
        return set()
    variants = {clean}
    without_parenthetical = re.sub(r"\s*\([^)]*\)\s*$", "", clean).strip()
    if without_parenthetical and len(without_parenthetical) >= 4:
        variants.add(without_parenthetical)
    return variants


def _find_forbidden_entity_mentions(
    normalized_query: dict[str, Any],
    forbidden_names: list[str],
    *,
    allowed_names: set[str],
) -> list[str]:
    text_parts = _flatten_strings(normalized_query)
    combined = "\n".join(text_parts)
    found: list[str] = []
    for name in forbidden_names:
        clean = str(name).strip()
        if not clean or clean in allowed_names or len(clean) < 4:
            continue
        pattern = _entity_name_pattern(clean)
        if pattern.search(combined):
            found.append(clean)
    return found


def _entity_name_pattern(name: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in re.split(r"\s+", str(name).strip()) if part]
    body = r"[\s\-]+".join(parts) if parts else re.escape(str(name).strip())
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)
