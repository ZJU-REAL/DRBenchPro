"""Helpers for enriching graph artifacts with derived structural fields."""

from __future__ import annotations

from collections import defaultdict

from drbenchpro.construction.graph.schemas import GraphData, RealizedNeighborLink


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _classify_underexpanded_reason(
    *,
    expected_max_neighbors: int,
    out_degree: int,
    expansion_record: dict[str, object] | None,
) -> str:
    """Explain why a node has fewer realized children than its configured fanout."""
    if expansion_record is None:
        return "missing_expansion_record"
    candidate_count = _as_int(expansion_record.get("candidate_count"), 0)
    selected_raw_count = _as_int(expansion_record.get("selected_raw_count"), 0)
    accepted_selected_count = _as_int(expansion_record.get("accepted_selected_count"), out_degree)
    if str(expansion_record.get("selection_error", "") or "").strip():
        return "llm_selection_failed_fallback_used"
    if candidate_count <= 0:
        return "no_available_candidates"
    if accepted_selected_count < selected_raw_count:
        if candidate_count < expected_max_neighbors:
            return "candidate_count_below_limit_and_duplicates_filtered"
        return "duplicates_filtered_by_existing_or_sibling_nodes"
    if candidate_count < expected_max_neighbors:
        return "candidate_count_below_limit"
    if selected_raw_count < min(candidate_count, expected_max_neighbors):
        return "selector_returned_fewer_than_available_candidates"
    return "accepted_selection_below_limit"


def attach_realized_out_neighbors(graph: GraphData) -> GraphData:
    """
    Populate each entity with actual graph out-links retained in the graph.

    This differs from `anchors`, which are raw outbound link candidates parsed
    according to the graph-build mode. `realized_out_neighbors` only records
    links that actually became graph edges in the built artifact.
    """
    entities_by_title = {entity.wikipedia_title: entity for entity in graph.entities}
    grouped: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)

    for edge in graph.edges:
        source = edge.source_title
        target = edge.target_title
        target_entity = entities_by_title.get(target)
        bucket = grouped[source].get(target)
        if bucket is None:
            bucket = {
                "target_title": target,
                "edge_ids": [],
                "anchor_texts": [],
                "paragraph_ids": [],
                "target_url": edge.target_url,
                "target_text_snippet": edge.target_text_snippet or (target_entity.text_snippet if target_entity else ""),
            }
            grouped[source][target] = bucket
        edge_ids = bucket["edge_ids"]
        if edge.edge_id and edge.edge_id not in edge_ids:
            edge_ids.append(edge.edge_id)
        anchor_texts = bucket["anchor_texts"]
        anchor_text = (edge.anchor_text or "").strip()
        if anchor_text and anchor_text not in anchor_texts:
            anchor_texts.append(anchor_text)
        paragraph_ids = bucket["paragraph_ids"]
        if edge.paragraph_id not in paragraph_ids:
            paragraph_ids.append(int(edge.paragraph_id))
        if not bucket["target_url"] and edge.target_url:
            bucket["target_url"] = edge.target_url
        if not bucket["target_text_snippet"]:
            bucket["target_text_snippet"] = edge.target_text_snippet or (target_entity.text_snippet if target_entity else "")

    for entity in graph.entities:
        realized = grouped.get(entity.wikipedia_title, {})
        entity.realized_out_neighbors = [
            RealizedNeighborLink(
                target_title=str(item["target_title"]),
                edge_ids=list(item["edge_ids"]),
                anchor_texts=list(item["anchor_texts"]),
                paragraph_ids=sorted(int(v) for v in item["paragraph_ids"]),
                target_url=str(item["target_url"]),
                target_text_snippet=str(item["target_text_snippet"]),
            )
            for item in realized.values()
        ]
    return graph


def build_compact_raw_graph_payload(graph: GraphData) -> dict[str, object]:
    """
    Build a compact raw graph payload for large-scale BFS graph generation.

    The compact payload keeps only the fields needed for structural expansion:
    - entity BFS id
    - wikipedia title
    - BFS depth
    - source id / source title
    - realized out-neighbor title set
    - edge evidence/details needed by later subgraph sampling and query prompts
    """
    attach_realized_out_neighbors(graph)
    metadata = graph.metadata
    entities = sorted(graph.entities, key=lambda item: item._id)
    entity_by_title = {entity.wikipedia_title: entity for entity in entities}
    raw_diagnostics = graph.diagnostics or {}
    expansion_records = (
        raw_diagnostics.get("node_expansion_records", {})
        if isinstance(raw_diagnostics, dict)
        else {}
    )
    if not isinstance(expansion_records, dict):
        expansion_records = {}

    compact_entities = []
    underexpanded_non_leaf_nodes: list[dict[str, object]] = []
    for entity in entities:
        out_titles: list[str] = []
        out_ids: list[str] = []
        out_edge_ids: list[str] = []
        for neighbor in entity.realized_out_neighbors:
            target_title = neighbor.target_title
            if not target_title:
                continue
            target_entity = entity_by_title.get(target_title)
            out_titles.append(target_title)
            out_ids.append(target_entity._id if target_entity is not None else "")
            out_edge_ids.append(str((neighbor.edge_ids or [""])[0]).strip())
        out_degree = len(out_titles)
        if int(entity.depth) == 0 and (
            bool(getattr(metadata, "root_expand_all_intro_links", False))
            or str(getattr(metadata, "root_link_selection_mode", "intro") or "intro") == "full_page_llm"
        ):
            expected_max_neighbors = None
        else:
            expected_max_neighbors = (
                int(metadata.root_max_neighbors_per_node)
                if int(entity.depth) == 0
                else int(metadata.max_neighbors_per_node)
            )
        if (
            entity.depth < int(metadata.max_depth)
            and expected_max_neighbors is not None
            and out_degree < expected_max_neighbors
        ):
            expansion_record = expansion_records.get(entity.wikipedia_title)
            if not isinstance(expansion_record, dict):
                expansion_record = None
            item = {
                "_id": entity._id,
                "wikipedia_title": entity.wikipedia_title,
                "depth": int(entity.depth),
                "source_id": entity.source_id,
                "source_title": entity.source_title,
                "out_degree": out_degree,
                "max_neighbors_per_node": expected_max_neighbors,
                "out_neighbor_titles": list(out_titles),
                "out_neighbor_ids": list(out_ids),
                "out_edge_ids": list(out_edge_ids),
                "underexpanded_reason": _classify_underexpanded_reason(
                    expected_max_neighbors=expected_max_neighbors,
                    out_degree=out_degree,
                    expansion_record=expansion_record,
                ),
            }
            if expansion_record is not None:
                item.update(
                    {
                        "candidate_count": _as_int(expansion_record.get("candidate_count"), 0),
                        "llm_candidate_count": _as_int(expansion_record.get("llm_candidate_count"), 0),
                        "selected_raw_count": _as_int(expansion_record.get("selected_raw_count"), 0),
                        "accepted_selected_count": _as_int(expansion_record.get("accepted_selected_count"), out_degree),
                        "neighbor_limit": _as_int(expansion_record.get("neighbor_limit"), expected_max_neighbors),
                        "used_llm": bool(expansion_record.get("used_llm", False)),
                        "selection_fallback": bool(expansion_record.get("selection_fallback", False)),
                        "selection_error": str(expansion_record.get("selection_error", "") or ""),
                        "selected_raw_titles": list(expansion_record.get("selected_raw_titles", []) or []),
                        "accepted_selected_titles": list(expansion_record.get("accepted_selected_titles", []) or []),
                    }
                )
            underexpanded_non_leaf_nodes.append(item)
        compact_entities.append(
            {
                "_id": entity._id,
                "wikipedia_title": entity.wikipedia_title,
                "depth": int(entity.depth),
                "source_id": entity.source_id,
                "source_title": entity.source_title,
                "out_neighbor_titles": out_titles,
                "out_neighbor_ids": out_ids,
                "out_edge_ids": out_edge_ids,
            }
        )
    compact_edges = [
        {
            "edge_id": edge.edge_id,
            "source_title": edge.source_title,
            "target_title": edge.target_title,
            "anchor_text": edge.anchor_text,
            "paragraph_id": int(edge.paragraph_id),
            "start": int(edge.start),
            "end": int(edge.end),
            "context_text": edge.context_text,
            "source_url": edge.source_url,
            "target_url": edge.target_url,
            "href": edge.href,
            "target_text_snippet": edge.target_text_snippet,
            "relation_label": edge.relation_label,
            "relation_explanation": edge.relation_explanation,
            "relation_mode": edge.relation_mode,
        }
        for edge in sorted(graph.edges, key=lambda item: item.edge_id)
    ]

    return {
        "metadata": {
            "graph_id": metadata.graph_id,
            "seed_entity": metadata.seed_entity,
            "max_nodes": metadata.max_nodes,
            "max_edges": metadata.max_edges,
            "max_depth": metadata.max_depth,
            "max_neighbors_per_node": metadata.max_neighbors_per_node,
            "root_max_neighbors_per_node": metadata.root_max_neighbors_per_node,
            "root_expand_all_intro_links": bool(getattr(metadata, "root_expand_all_intro_links", False)),
            "root_link_selection_mode": str(getattr(metadata, "root_link_selection_mode", "intro") or "intro"),
            "root_full_page_prompt_file": str(
                getattr(metadata, "root_full_page_prompt_file", "graph_root_full_page_selection.md")
                or "graph_root_full_page_selection.md"
            ),
            "root_full_page_llm_target_neighbors": int(getattr(metadata, "root_full_page_llm_target_neighbors", 30) or 0),
            "neighbor_selection_mode": str(getattr(metadata, "neighbor_selection_mode", "heuristic")),
            "relation_mode": str(getattr(metadata, "relation_mode", "none") or "none"),
            "llm_candidate_pool_limit": int(getattr(metadata, "llm_candidate_pool_limit", 0) or 0),
            "root_llm_candidate_pool_limit": int(
                getattr(
                    metadata,
                    "root_llm_candidate_pool_limit",
                    getattr(metadata, "llm_candidate_pool_limit", 0),
                )
                or 0
            ),
            "page_fetch_concurrency": int(getattr(metadata, "page_fetch_concurrency", 1) or 1),
            "page_fetch_attempt_stats": dict(getattr(metadata, "page_fetch_attempt_stats", {}) or {}),
            "build_time_utc": metadata.build_time_utc,
            "output_style": "compact_raw",
            "entity_count": len(compact_entities),
            "edge_count": len(graph.edges),
            "underexpanded_non_leaf_count": len(underexpanded_non_leaf_nodes),
        },
        "entities": compact_entities,
        "edges": compact_edges,
        "diagnostics": {
            "underexpanded_non_leaf_nodes": underexpanded_non_leaf_nodes,
            "node_expansion_records": dict(expansion_records),
            "page_fetch_attempt_records": list(
                raw_diagnostics.get("page_fetch_attempt_records", [])
                if isinstance(raw_diagnostics, dict)
                else []
            ),
        },
    }
