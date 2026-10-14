"""Pipeline entry for generating one query artifact per sampled subgraph."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from drbenchpro.construction.generation.query_generator_llm import (
    QUERY_PROMPT_FILE,
    normalize_query_gen_mode,
    resolve_query_prompt_file_for_mode,
)
from drbenchpro.construction.stages.build_sample import build_query_payload
from drbenchpro.common.io import dump_json, load_json, utc_now_iso


def _load_query_inputs_payload(subgraphs_path: str | Path) -> dict[str, Any]:
    """Normalize selected_subgraphs/query_generation_inputs into one query-input payload."""
    payload = load_json(subgraphs_path)
    if isinstance(payload.get("query_generation_inputs"), list):
        return payload

    selected = payload.get("selected_subgraphs", [])
    if not isinstance(selected, list):
        raise ValueError("Expected `selected_subgraphs` or `query_generation_inputs` in payload.")

    query_inputs: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        query_inputs.append(
            {
                "subgraph_id": str(item.get("subgraph_id", "")).strip(),
                "subgraph_type": str(item.get("subgraph_type", "")).strip(),
                "anchor_node": str(item.get("anchor_node", "")).strip(),
                "depth_1_node_titles": item.get("depth_1_node_titles"),
                "depth_1_node_ids": item.get("depth_1_node_ids"),
                "depth_1_branch_selection_reasons": item.get("depth_1_branch_selection_reasons"),
                "selection_notes": item.get("selection_notes"),
                "query_spec": item.get("query_spec"),
                "query_materials": item.get("query_materials"),
            }
        )

    return {
        "metadata": dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {},
        "query_generation_inputs": query_inputs,
    }


def _build_graph_like_payload(base_metadata: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Convert one query-generation input into the graph-like payload expected by query generators."""
    query_spec = item.get("query_spec", {}) if isinstance(item.get("query_spec"), dict) else {}
    query_materials = item.get("query_materials", {}) if isinstance(item.get("query_materials"), dict) else {}

    subgraph_id = str(item.get("subgraph_id", "")).strip()
    anchor_topic = str(query_spec.get("anchor_topic", "")).strip() or str(item.get("anchor_node", "")).strip()

    entities: list[dict[str, Any]] = []
    for idx, node in enumerate(query_materials.get("nodes", []) or [], start=1):
        if not isinstance(node, dict):
            continue
        title = str(node.get("wikipedia_title", "")).strip()
        if not title:
            continue
        node_id = (
            str(node.get("node_id", "")).strip()
            or str(node.get("entity_id", "")).strip()
            or str(node.get("_id", "")).strip()
            or f"node_{idx:04d}"
        )
        entities.append(
            {
                "node_id": node_id,
                "wikipedia_title": title,
                "text_snippet": str(node.get("text_snippet", "")).strip(),
                "text": list(node.get("text", []) or []),
                "root_depth": node.get("root_depth"),
            }
        )

    edges: list[dict[str, Any]] = []
    for edge in query_materials.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        source_title = str(edge.get("source_title", "")).strip()
        target_title = str(edge.get("target_title", "")).strip()
        if not source_title or not target_title:
            continue
        edges.append(
            {
                "edge_id": str(edge.get("edge_id", "")).strip(),
                "edge_depth": edge.get("edge_depth"),
                "source_title": source_title,
                "target_title": target_title,
                "relation_label": str(edge.get("relation_label", "")).strip(),
                "relation_explanation": str(edge.get("relation_explanation", "")).strip(),
                "anchor_text": str(edge.get("anchor_text", "")).strip(),
                "context_text": str(edge.get("context_text", "")).strip(),
            }
        )

    graph_id = str(base_metadata.get("graph_id", "")).strip()
    return {
        "metadata": {
            "graph_id": f"{graph_id}::{subgraph_id}" if graph_id else subgraph_id,
            "seed_entity": anchor_topic or str(base_metadata.get("seed_entity", "")).strip(),
            "language": str(base_metadata.get("language", "en")).strip() or "en",
            "relation_mode": str(base_metadata.get("relation_mode", "none")).strip() or "none",
            "artifact_type": "subgraph_query_input",
            "subgraph_id": subgraph_id,
            "subgraph_type": str(item.get("subgraph_type", "")).strip(),
            "depth_1_node_titles": item.get("depth_1_node_titles"),
            "depth_1_node_ids": item.get("depth_1_node_ids"),
            "depth_1_branch_selection_reasons": item.get("depth_1_branch_selection_reasons"),
        },
        "entities": entities,
        "edges": edges,
    }


def run_subgraph_query_generation(
    subgraphs_path: str | Path,
    output_dir: str | Path,
    query_mode: str,
    llm_settings: dict[str, Any],
    llm_model_override: str | None = None,
    query_gen_mode: str = "less",
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    resume_payload: dict[str, Any] | None = None,
    partial_hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    query_prompt_file: str | None = None,
) -> dict[str, str]:
    """Generate one query artifact per sampled subgraph."""
    query_gen_mode = normalize_query_gen_mode(query_gen_mode or "less")
    prompt_filename = resolve_query_prompt_file_for_mode(query_prompt_file or QUERY_PROMPT_FILE, query_gen_mode=query_gen_mode)
    payload = _load_query_inputs_payload(subgraphs_path)
    metadata = dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {}
    items = payload.get("query_generation_inputs", []) or []
    if not isinstance(items, list):
        raise ValueError("`query_generation_inputs` must be a list.")
    resume_payload = resume_payload or {}

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "subgraph_queries.json"

    resume_queries = resume_payload.get("queries", []) if isinstance(resume_payload.get("queries"), list) else []
    resume_by_subgraph_id: dict[str, dict[str, Any]] = {}
    for item in resume_queries:
        if not isinstance(item, dict):
            continue
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        if subgraph_id:
            resume_by_subgraph_id[subgraph_id] = dict(item)

    total = len(items)
    query_records_by_subgraph_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        if subgraph_id and subgraph_id in resume_by_subgraph_id:
            query_records_by_subgraph_id[subgraph_id] = resume_by_subgraph_id[subgraph_id]
    completed = len(query_records_by_subgraph_id)
    for item in items:
        if not isinstance(item, dict):
            continue
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        if subgraph_id and subgraph_id in resume_by_subgraph_id:
            continue

    pending_items = [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("subgraph_id", "")).strip()
        and str(item.get("subgraph_id", "")).strip() not in resume_by_subgraph_id
    ]

    def _generate_one_query_record(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        subgraph_id = str(item.get("subgraph_id", "")).strip()
        graph_like = _build_graph_like_payload(metadata, item)
        local_progress_hook = None
        if progress_hook is not None or attempt_hook is not None:

            def _local_progress_hook(stage: str, message: str, fields: dict[str, Any]) -> None:
                payload = {
                    "stage": stage,
                    "message": message,
                    "subgraph_id": subgraph_id,
                    "subgraph_type": str(item.get("subgraph_type", "")).strip(),
                    "anchor_node": str(item.get("anchor_node", "")).strip(),
                    **dict(fields),
                }
                if stage in {"query_llm_attempt_failed", "query_llm_attempt_failed_terminal"} and attempt_hook is not None:
                    attempt_hook(payload)
                elif progress_hook is not None:
                    progress_hook(payload)

            local_progress_hook = _local_progress_hook

        query_payload = build_query_payload(
            graph=graph_like,
            query_mode=query_mode,
            config={"llm": llm_settings},
            llm_model_override=llm_model_override,
            progress_hook=local_progress_hook,
            query_gen_mode=query_gen_mode,
            query_prompt_file=prompt_filename,
        )
        query_text = str(query_payload.get("query", "")).strip()
        query_id = f"query_{subgraph_id}" if subgraph_id else ""
        query_metadata = dict(query_payload)
        query_metadata.pop("query", None)
        if query_id:
            query_metadata["query_id"] = query_id
        query_record = {
            "query_id": query_id,
            "subgraph_id": subgraph_id,
            "subgraph_type": str(item.get("subgraph_type", "")).strip(),
            "anchor_node": str(item.get("anchor_node", "")).strip(),
            "depth_1_node_titles": item.get("depth_1_node_titles"),
            "depth_1_node_ids": item.get("depth_1_node_ids"),
            "depth_1_branch_selection_reasons": item.get("depth_1_branch_selection_reasons"),
            "selection_notes": item.get("selection_notes"),
            "query_spec": item.get("query_spec"),
            "query": query_text,
            "query_metadata": query_metadata,
        }
        return subgraph_id, query_record

    def _after_query_record(subgraph_id: str, query_record: dict[str, Any]) -> None:
        nonlocal completed
        query_records_by_subgraph_id[subgraph_id] = query_record
        completed += 1
        if progress_hook is not None:
            progress_hook(
                {
                    "stage": "subgraph_query_done",
                    "message": "Subgraph query completed",
                    "completed": completed,
                    "total": total,
                    "subgraph_id": subgraph_id,
                    "subgraph_type": str(query_record.get("subgraph_type", "")).strip(),
                    "anchor_node": str(query_record.get("anchor_node", "")).strip(),
                    "status": "ok",
                    "query_chars": len(str(query_record.get("query", ""))),
                }
            )
        partial_result = {
            "metadata": {
                "artifact_type": "subgraph_queries",
                "source_subgraphs_path": str(Path(subgraphs_path).resolve()),
                "graph_id": str(metadata.get("graph_id", "")).strip(),
                "seed_entity": str(metadata.get("seed_entity", "")).strip(),
                "query_mode": str(query_mode).strip(),
                "query_gen_mode": query_gen_mode,
                "query_prompt_file": prompt_filename if query_mode == "llm" else "",
                "build_time_utc": utc_now_iso(),
                "subgraph_count": len(query_records_by_subgraph_id),
            },
            "queries": [
                query_records_by_subgraph_id[str(row.get("subgraph_id", "")).strip()]
                for row in items
                if isinstance(row, dict) and str(row.get("subgraph_id", "")).strip() in query_records_by_subgraph_id
            ],
        }
        if partial_hook is not None:
            partial_hook(
                partial_result,
                {
                    "phase": "queries",
                    "completed_queries": completed,
                    "total_queries": total,
                    "subgraph_id": subgraph_id,
                },
            )

    query_workers = 1
    if query_mode == "llm":
        query_workers = max(1, int(llm_settings.get("concurrency", 1)))
    query_workers = min(query_workers, len(pending_items)) if pending_items else 1

    if query_workers > 1:
        with ThreadPoolExecutor(max_workers=query_workers) as executor:
            futures = {
                executor.submit(_generate_one_query_record, item): str(item.get("subgraph_id", "")).strip()
                for item in pending_items
            }
            for future in as_completed(futures):
                subgraph_id, query_record = future.result()
                _after_query_record(subgraph_id, query_record)
    else:
        for item in pending_items:
            subgraph_id, query_record = _generate_one_query_record(item)
            _after_query_record(subgraph_id, query_record)

    queries = [
        query_records_by_subgraph_id[str(item.get("subgraph_id", "")).strip()]
        for item in items
        if isinstance(item, dict) and str(item.get("subgraph_id", "")).strip() in query_records_by_subgraph_id
    ]
    result = {
        "metadata": {
            "artifact_type": "subgraph_queries",
            "source_subgraphs_path": str(Path(subgraphs_path).resolve()),
            "graph_id": str(metadata.get("graph_id", "")).strip(),
            "seed_entity": str(metadata.get("seed_entity", "")).strip(),
            "query_mode": str(query_mode).strip(),
            "query_gen_mode": query_gen_mode,
            "query_prompt_file": prompt_filename if query_mode == "llm" else "",
            "build_time_utc": utc_now_iso(),
            "subgraph_count": len(queries),
        },
        "queries": queries,
    }
    dump_json(output_path, result)
    return {"subgraph_queries_json": str(output_path)}
