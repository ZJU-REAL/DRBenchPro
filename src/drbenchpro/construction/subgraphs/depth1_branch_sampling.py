"""Depth-1 branch-tree subgraph sampling for compact raw wiki graphs.

Each sampled subgraph is the root node plus a selected bundle of depth-1
branches and the complete descendant tree under those branches.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re
import random
from typing import Any, Callable

from drbenchpro.construction.graph.schemas import EntityNode, GraphData
from drbenchpro.construction.subgraphs.query_interface import generate_query_text_from_spec
from drbenchpro.construction.subgraphs.schemas import (
    QueryMaterials,
    QuerySpec,
    SampledSubgraph,
    SamplingArtifacts,
    SamplingMetadata,
    SubgraphEdgeMaterial,
    SubgraphEdgeView,
    SubgraphNodeMaterial,
)
from drbenchpro.common.ids import make_ordered_subgraph_id
from drbenchpro.common.io import utc_now_iso
from drbenchpro.llm.client import LLMConfig, OpenAIChatClient, invalidate_cache_path, parse_first_json_object
from drbenchpro.common.resources import load_prompt_spec, render_user_prompt
from drbenchpro.common.text import safe_snippet


DEPTH1_SELECTION_PROMPT_FILE = "subgraph_depth1_branch_selection.md"


@dataclass(init=False)
class Depth1BranchSamplingConfig:
    """Configuration for depth-1 branch-tree sampling from compact raw graphs."""

    target_subgraphs: int
    depth1_branches_per_subgraph: int
    random_seed: int
    leaf_depth: int | None
    max_attempts_multiplier: int
    selection_mode: str
    llm_settings: dict[str, Any] | None = field(default=None, repr=False)
    attempt_hook: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)

    def __init__(
        self,
        target_subgraphs: int = 10,
        depth1_branches_per_subgraph: int | None = None,
        *,
        paths_per_subgraph: int | None = None,
        random_seed: int = 0,
        leaf_depth: int | None = None,
        max_attempts_multiplier: int = 50,
        selection_mode: str = "llm",
        llm_settings: dict[str, Any] | None = None,
        attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        branch_count = depth1_branches_per_subgraph if depth1_branches_per_subgraph is not None else paths_per_subgraph
        self.target_subgraphs = int(target_subgraphs)
        self.depth1_branches_per_subgraph = int(branch_count if branch_count is not None else 10)
        self.random_seed = int(random_seed)
        self.leaf_depth = leaf_depth
        self.max_attempts_multiplier = int(max_attempts_multiplier)
        self.selection_mode = str(selection_mode)
        self.llm_settings = llm_settings
        self.attempt_hook = attempt_hook

    @property
    def paths_per_subgraph(self) -> int:
        """Expose the configured depth-1 branch count as paths_per_subgraph."""
        return self.depth1_branches_per_subgraph

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_subgraphs": self.target_subgraphs,
            "depth1_branches_per_subgraph": self.depth1_branches_per_subgraph,
            "depth_1_branches_per_subgraph": self.depth1_branches_per_subgraph,
            "paths_per_subgraph": self.depth1_branches_per_subgraph,
            "random_seed": self.random_seed,
            "leaf_depth": self.leaf_depth,
            "max_attempts_multiplier": self.max_attempts_multiplier,
            "selection_mode": self.selection_mode,
            "selection_llm_model": str((self.llm_settings or {}).get("model", "")).strip(),
            "selection_llm_base_url": str((self.llm_settings or {}).get("base_url", "")).strip(),
        }


def _load_graph(graph_payload: dict[str, Any]) -> GraphData:
    return GraphData.from_dict(graph_payload)


def _root_node(graph: GraphData) -> EntityNode:
    roots = [entity for entity in graph.entities if int(entity.depth) == 0]
    if roots:
        return sorted(roots, key=lambda node: node._id)[0]
    if not graph.entities:
        raise ValueError("Graph has no entities; cannot sample subgraphs.")
    return sorted(graph.entities, key=lambda node: (int(node.depth), node._id))[0]


def _depth1_nodes(graph: GraphData, root: EntityNode) -> list[EntityNode]:
    nodes = [
        entity
        for entity in graph.entities
        if int(entity.depth) == 1 and (not entity.source_id or entity.source_id == root._id)
    ]
    if nodes:
        return sorted(nodes, key=lambda node: node._id)

    root_title = str(root.wikipedia_title).strip()
    depth1_titles = {
        str(edge.target_title).strip()
        for edge in graph.edges
        if str(edge.source_title).strip() == root_title and str(edge.target_title).strip()
    }
    return sorted(
        [entity for entity in graph.entities if int(entity.depth) == 1 and entity.wikipedia_title in depth1_titles],
        key=lambda node: node._id,
    )


def _children_by_source_id(graph: GraphData) -> dict[str, list[EntityNode]]:
    children: dict[str, list[EntityNode]] = defaultdict(list)
    for entity in graph.entities:
        source_id = str(entity.source_id or "").strip()
        if source_id:
            children[source_id].append(entity)
    for values in children.values():
        values.sort(key=lambda node: node._id)
    return children


def _descendant_ids(branch: EntityNode, children_by_id: dict[str, list[EntityNode]]) -> set[str]:
    selected: set[str] = set()
    stack = [branch]
    while stack:
        node = stack.pop()
        if node._id in selected:
            continue
        selected.add(node._id)
        stack.extend(reversed(children_by_id.get(node._id, [])))
    return selected


def _longest_root_path(root: EntityNode, selected_ids: set[str], children_by_id: dict[str, list[EntityNode]]) -> list[str]:
    best: list[EntityNode] = [root]

    def visit(node: EntityNode, path: list[EntityNode]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        for child in children_by_id.get(node._id, []):
            if child._id in selected_ids:
                visit(child, [*path, child])

    visit(root, [root])
    return [node.wikipedia_title for node in best]


def _edge_target_depth(edge: Any, entities_by_title: dict[str, EntityNode]) -> int:
    target = entities_by_title.get(str(edge.target_title).strip())
    if target is None:
        return 0
    return int(target.depth)


def _node_material(node: EntityNode) -> SubgraphNodeMaterial:
    return SubgraphNodeMaterial(
        wikipedia_title=node.wikipedia_title,
        node_id=node._id,
        url=node.url,
        text_snippet=node.text_snippet,
        text=list(node.text),
        root_depth=int(node.depth),
        realized_out_neighbors=list(node.realized_out_neighbors),
    )


def _branch_selection_reason_rows(
    depth1_nodes: list[EntityNode],
    selection_note: dict[str, Any] | None,
) -> list[dict[str, str]]:
    note = dict(selection_note or {})
    selected_candidate_ids = _extract_string_list(
        note.get("selected_candidate_ids") or note.get("llm_selected_candidate_ids")
    )
    reasons_by_candidate_id = (
        note.get("llm_branch_selection_reasons_by_candidate_id")
        if isinstance(note.get("llm_branch_selection_reasons_by_candidate_id"), dict)
        else {}
    )
    reasons_by_title = (
        note.get("llm_branch_selection_reasons_by_title")
        if isinstance(note.get("llm_branch_selection_reasons_by_title"), dict)
        else {}
    )
    root_relations_by_candidate_id = (
        note.get("llm_branch_root_relations_by_candidate_id")
        if isinstance(note.get("llm_branch_root_relations_by_candidate_id"), dict)
        else {}
    )
    root_relations_by_title = (
        note.get("llm_branch_root_relations_by_title")
        if isinstance(note.get("llm_branch_root_relations_by_title"), dict)
        else {}
    )
    group_rationale = str(note.get("llm_selection_rationale", "")).strip()
    fallback_reason = str(note.get("fallback_reason", "")).strip()
    selection_mode = str(note.get("selection_mode", "")).strip().lower()

    rows: list[dict[str, str]] = []
    for idx, node in enumerate(depth1_nodes):
        candidate_id = selected_candidate_ids[idx] if idx < len(selected_candidate_ids) else ""
        reason = str(reasons_by_candidate_id.get(candidate_id, "")).strip() if candidate_id else ""
        if not reason:
            reason = str(reasons_by_title.get(node.wikipedia_title, "")).strip()
        if not reason and group_rationale:
            reason = f"Selected as part of this coherent depth-1 branch group. Group rationale: {group_rationale}"
        if not reason and fallback_reason:
            reason = f"Selected by deterministic fallback after LLM depth-1 branch selection was unavailable: {fallback_reason}"
        if not reason and selection_mode and selection_mode != "llm":
            reason = "Selected by deterministic depth-1 branch sampling; no LLM per-branch rationale was requested."
        root_relation = str(root_relations_by_candidate_id.get(candidate_id, "")).strip() if candidate_id else ""
        if not root_relation:
            root_relation = str(root_relations_by_title.get(node.wikipedia_title, "")).strip()
        rows.append(
            {
                "candidate_id": candidate_id,
                "node_id": node._id,
                "wikipedia_title": node.wikipedia_title,
                "root_relation": root_relation,
                "selection_reason": reason,
            }
        )
    return rows


def _build_depth1_branch_subgraph(
    graph: GraphData,
    root: EntityNode,
    depth1_nodes: list[EntityNode],
    selection_note: dict[str, Any] | None = None,
) -> SampledSubgraph:
    entities_by_id = {entity._id: entity for entity in graph.entities}
    entities_by_title = {entity.wikipedia_title: entity for entity in graph.entities}
    children_by_id = _children_by_source_id(graph)

    selected_ids: set[str] = {root._id}
    descendants_by_branch: dict[str, list[str]] = {}
    descendants_by_branch_title: dict[str, list[str]] = {}
    for branch in depth1_nodes:
        branch_ids = _descendant_ids(branch, children_by_id)
        selected_ids.update(branch_ids)
        branch_descendants = [
            entities_by_id[node_id].wikipedia_title
            for node_id in sorted(branch_ids, key=lambda node_id: (entities_by_id[node_id].depth, node_id))
            if node_id != branch._id
        ]
        descendants_by_branch[branch._id] = branch_descendants
        descendants_by_branch_title[branch.wikipedia_title] = branch_descendants

    ordered_nodes = sorted(
        [entities_by_id[node_id] for node_id in selected_ids if node_id in entities_by_id],
        key=lambda node: (int(node.depth), node._id),
    )
    node_titles = [node.wikipedia_title for node in ordered_nodes]
    depth_1_node_titles = [node.wikipedia_title for node in depth1_nodes]
    depth_1_node_ids = [node._id for node in depth1_nodes]
    depth_1_branch_selection_reasons = _branch_selection_reason_rows(depth1_nodes, selection_note)
    selected_titles = set(node_titles)

    edge_views: list[SubgraphEdgeView] = []
    edge_materials: list[SubgraphEdgeMaterial] = []
    for edge in graph.edges:
        source_title = str(edge.source_title).strip()
        target_title = str(edge.target_title).strip()
        if source_title not in selected_titles or target_title not in selected_titles:
            continue
        target = entities_by_title.get(target_title)
        if target is not None and target._id not in selected_ids:
            continue
        edge_depth = _edge_target_depth(edge, entities_by_title)
        edge_views.append(
            SubgraphEdgeView(
                edge_id=edge.edge_id,
                source_title=edge.source_title,
                target_title=edge.target_title,
                edge_depth=edge_depth,
                relation_label=edge.relation_label or "",
                relation_explanation=edge.relation_explanation or "",
                anchor_text=edge.anchor_text or "",
            )
        )
        edge_materials.append(
            SubgraphEdgeMaterial(
                edge_id=edge.edge_id,
                source_title=edge.source_title,
                target_title=edge.target_title,
                edge_depth=edge_depth,
                anchor_text=edge.anchor_text or "",
                paragraph_id=int(edge.paragraph_id),
                context_text=edge.context_text or "",
                source_url=edge.source_url or "",
                target_url=edge.target_url or "",
                href=edge.href or "",
                source_text_snippet=entities_by_title.get(source_title, root).text_snippet,
                target_text_snippet=edge.target_text_snippet or "",
                relation_label=edge.relation_label or "",
                relation_explanation=edge.relation_explanation or "",
            )
        )

    layer_map = {node.wikipedia_title: int(node.depth) for node in ordered_nodes}
    subgraph_id = ""
    max_depth = max((int(node.depth) for node in ordered_nodes), default=0)

    query_spec = QuerySpec(
        subgraph_id=subgraph_id,
        subgraph_type="depth1_branch_tree",
        anchor_topic=graph.metadata.seed_entity,
        focus_nodes=depth_1_node_titles,
        focus_edges=[
            {
                "edge_id": edge.edge_id,
                "source_title": edge.source_title,
                "target_title": edge.target_title,
                "edge_depth": edge.edge_depth,
            }
            for edge in edge_views
        ],
        depth_1_node_titles=depth_1_node_titles,
        depth_1_node_ids=depth_1_node_ids,
        excluded_nodes=[],
        intended_user_intent=(
            "synthesize several semantically related first-hop branches under one seed topic "
            "and use their descendant trees as supporting research context"
        ),
        desired_answer_scope={
            "mode": "depth1_branch_tree",
            "root_topic": graph.metadata.seed_entity,
            "depth_1_branch_count": len(depth1_nodes),
            "depth_1_node_titles": depth_1_node_titles,
            "depth_1_node_ids": depth_1_node_ids,
            "depth_1_branch_selection_reasons": depth_1_branch_selection_reasons,
            "descendant_tree": "include all sampled descendants under the selected depth-1 branches",
        },
        breadth_depth_profile={
            "structure": "root_plus_selected_depth1_branch_trees",
            "depth_1_branch_count": len(depth1_nodes),
            "max_depth": max_depth,
        },
        graph_stats={
            "node_count": len(ordered_nodes),
            "edge_count": len(edge_views),
            "depth_1_node_count": len(depth1_nodes),
            "max_depth": max_depth,
        },
    )
    query_materials = QueryMaterials(
        nodes=[_node_material(node) for node in ordered_nodes],
        edges=edge_materials,
    )

    return SampledSubgraph(
        subgraph_id=subgraph_id,
        subgraph_type="depth1_branch_tree",
        anchor_node=graph.metadata.seed_entity,
        node_titles=node_titles,
        depth_1_node_titles=depth_1_node_titles,
        depth_1_node_ids=depth_1_node_ids,
        depth_1_branch_selection_reasons=depth_1_branch_selection_reasons,
        edges=edge_views,
        layer_map=layer_map,
        main_path=_longest_root_path(root, selected_ids, children_by_id),
        template_metadata={
            "sampled_depth_1_node_ids": depth_1_node_ids,
            "sampled_depth_1_node_titles": depth_1_node_titles,
            "depth_1_branch_selection_reasons": depth_1_branch_selection_reasons,
            "depth_1_branch_selection_reasons_by_node_id": {
                item["node_id"]: item["selection_reason"] for item in depth_1_branch_selection_reasons
            },
            "depth_1_branch_selection_reasons_by_title": {
                item["wikipedia_title"]: item["selection_reason"] for item in depth_1_branch_selection_reasons
            },
            "depth_1_branch_root_relations_by_node_id": {
                item["node_id"]: item.get("root_relation", "") for item in depth_1_branch_selection_reasons
            },
            "depth_1_branch_root_relations_by_title": {
                item["wikipedia_title"]: item.get("root_relation", "") for item in depth_1_branch_selection_reasons
            },
            "depth_1_descendants_by_node_id": descendants_by_branch,
            "depth_1_descendants_by_title": descendants_by_branch_title,
        },
        graph_stats=query_spec.graph_stats,
        scores={"overall": 1.0},
        normalized_scores={"overall": 1.0},
        filter_reasons=[],
        selection_status="selected",
        selection_notes=dict(selection_note or {}),
        query_spec=query_spec,
        query_text_placeholder=generate_query_text_from_spec(query_spec),
        query_materials=query_materials,
    )


def _root_edge_for_branch(graph: GraphData, root: EntityNode, branch: EntityNode) -> Any | None:
    root_title = str(root.wikipedia_title).strip()
    branch_title = str(branch.wikipedia_title).strip()
    for edge in graph.edges:
        if str(edge.source_title).strip() == root_title and str(edge.target_title).strip() == branch_title:
            return edge
    return None


def _candidate_root_relation_payload(graph: GraphData, root: EntityNode, branch: EntityNode) -> dict[str, str]:
    edge = _root_edge_for_branch(graph, root, branch)
    if edge is None:
        return {
            "edge_id": "",
            "relation_label": "",
            "relation_explanation": "",
            "context_text": "",
        }
    return {
        "edge_id": str(edge.edge_id or ""),
        "relation_label": str(edge.relation_label or ""),
        "relation_explanation": safe_snippet(str(edge.relation_explanation or ""), max_chars=260),
        "context_text": safe_snippet(str(edge.context_text or ""), max_chars=320),
    }


def _candidate_root_relation_summary(candidate: dict[str, Any]) -> str:
    direct_explanation = str(candidate.get("root_relation_explanation", "")).strip()
    if direct_explanation:
        return direct_explanation
    relation = candidate.get("root_relation")
    if not isinstance(relation, dict):
        return ""
    explanation = str(relation.get("relation_explanation", "")).strip()
    if explanation:
        return explanation
    label = str(relation.get("relation_label", "")).strip()
    context = str(relation.get("context_text", "")).strip()
    if label and context:
        return f"{label}: {context}"
    if label:
        return label
    if context:
        return context
    return ""


def _candidate_payloads(
    graph: GraphData,
    root: EntityNode,
    depth1_nodes: list[EntityNode],
    children_by_id: dict[str, list[EntityNode]],
) -> tuple[list[dict[str, Any]], dict[str, EntityNode]]:
    candidates: list[dict[str, Any]] = []
    by_candidate_id: dict[str, EntityNode] = {}
    for idx, node in enumerate(depth1_nodes, start=1):
        candidate_id = f"d1_{idx:03d}"
        by_candidate_id[candidate_id] = node
        children = children_by_id.get(node._id, [])
        descendants = _descendant_ids(node, children_by_id)
        root_relation = _candidate_root_relation_payload(graph, root, node)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "node_id": node._id,
                "wikipedia_title": node.wikipedia_title,
                "summary": safe_snippet(node.text_snippet or " ".join(node.text[:1]), max_chars=320),
                "root_relation_explanation": root_relation.get("relation_explanation", ""),
                "root_relation": root_relation,
                "direct_children": [child.wikipedia_title for child in children[:8]],
                "descendant_count": max(0, len(descendants) - 1),
            }
        )
    return candidates, by_candidate_id


def _extract_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_extract_string_list(item))
        return items
    if isinstance(value, dict):
        for key in ("candidate_id", "id", "node_id", "wikipedia_title", "title"):
            if key in value:
                return _extract_string_list(value.get(key))
    return []


def _raw_group_ids(raw_group: Any) -> list[str]:
    if isinstance(raw_group, dict):
        for key in (
            "selected_candidate_ids",
            "candidate_ids",
            "depth_1_candidate_ids",
            "selected_depth_1_candidate_ids",
            "selected_branches",
            "selected_titles",
            "titles",
        ):
            values = _extract_string_list(raw_group.get(key))
            if values:
                return values
    return _extract_string_list(raw_group)


def _candidate_lookup_maps(candidates: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    title_to_candidate_id = {
        str(candidate.get("wikipedia_title", "")).strip(): str(candidate.get("candidate_id", "")).strip()
        for candidate in candidates
        if str(candidate.get("candidate_id", "")).strip() and str(candidate.get("wikipedia_title", "")).strip()
    }
    node_id_to_candidate_id = {
        str(candidate.get("node_id", "")).strip(): str(candidate.get("candidate_id", "")).strip()
        for candidate in candidates
        if str(candidate.get("candidate_id", "")).strip() and str(candidate.get("node_id", "")).strip()
    }
    return title_to_candidate_id, node_id_to_candidate_id


def _candidate_titles_by_id(candidates: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(candidate.get("candidate_id", "")).strip(): str(candidate.get("wikipedia_title", "")).strip()
        for candidate in candidates
        if str(candidate.get("candidate_id", "")).strip()
    }


_TITLE_SIMILARITY_STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
}


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(title).lower())
        if token and token not in _TITLE_SIMILARITY_STOPWORDS
    }


def _titles_are_near_duplicate(left: str, right: str) -> bool:
    left_text = " ".join(str(left).lower().split())
    right_text = " ".join(str(right).lower().split())
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_tokens = _title_tokens(left_text)
    right_tokens = _title_tokens(right_text)
    if not left_tokens or not right_tokens:
        return False
    smaller = min(len(left_tokens), len(right_tokens))
    if smaller >= 2 and (left_tokens <= right_tokens or right_tokens <= left_tokens):
        return True
    jaccard = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return jaccard >= 0.72


def _is_near_duplicate_candidate(candidate_id: str, selected: list[str], titles_by_id: dict[str, str]) -> bool:
    title = titles_by_id.get(candidate_id, "")
    return any(_titles_are_near_duplicate(title, titles_by_id.get(existing_id, "")) for existing_id in selected)


def _dedupe_homogeneous_group(
    selected: list[str],
    *,
    candidates: list[dict[str, Any]],
    usage: Counter[str],
    group_size: int,
) -> list[str]:
    """Prefer non-redundant title coverage inside one depth-1 branch group."""
    titles_by_id = _candidate_titles_by_id(candidates)
    candidate_ids = [str(candidate.get("candidate_id", "")).strip() for candidate in candidates]
    result: list[str] = []
    seen: set[str] = set()
    rejected: set[str] = set()
    for candidate_id in selected:
        if not candidate_id or candidate_id in seen:
            continue
        if _is_near_duplicate_candidate(candidate_id, result, titles_by_id):
            rejected.add(candidate_id)
            continue
        result.append(candidate_id)
        seen.add(candidate_id)
        if len(result) >= group_size:
            return result[:group_size]

    replacement_pool = sorted(
        [candidate_id for candidate_id in candidate_ids if candidate_id not in seen and candidate_id not in rejected],
        key=lambda candidate_id: (usage[candidate_id], candidate_id),
    )
    for candidate_id in replacement_pool:
        if _is_near_duplicate_candidate(candidate_id, result, titles_by_id):
            continue
        result.append(candidate_id)
        seen.add(candidate_id)
        if len(result) >= group_size:
            return result[:group_size]

    for candidate_id in candidate_ids:
        if candidate_id not in seen:
            result.append(candidate_id)
            seen.add(candidate_id)
        if len(result) >= group_size:
            break
    return result[:group_size]


def _candidate_id_from_reference(
    value: Any,
    *,
    by_candidate_id: dict[str, EntityNode],
    title_to_candidate_id: dict[str, str],
    node_id_to_candidate_id: dict[str, str],
) -> str:
    if isinstance(value, dict):
        for key in ("candidate_id", "id", "depth_1_candidate_id", "selected_candidate_id"):
            candidate_id = str(value.get(key, "")).strip()
            if candidate_id in by_candidate_id:
                return candidate_id
        for key in ("node_id", "depth_1_node_id"):
            candidate_id = node_id_to_candidate_id.get(str(value.get(key, "")).strip(), "")
            if candidate_id:
                return candidate_id
        for key in ("wikipedia_title", "title", "depth_1_node_title"):
            candidate_id = title_to_candidate_id.get(str(value.get(key, "")).strip(), "")
            if candidate_id:
                return candidate_id
        return ""

    text = str(value).strip()
    if text in by_candidate_id:
        return text
    return title_to_candidate_id.get(text, "") or node_id_to_candidate_id.get(text, "")


def _reason_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("reason", "selection_reason", "rationale", "why_selected", "explanation", "notes"):
            text = str(value.get(key, "")).strip()
            if text:
                return text
    return ""


def _branch_text_field(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in keys:
            text = str(value.get(key, "")).strip()
            if text:
                return text
    return ""


def _merge_branch_detail(
    details: dict[str, dict[str, str]],
    candidate_id: str,
    value: Any,
) -> None:
    if not candidate_id:
        return
    row = details.setdefault(candidate_id, {})
    reason = _reason_text(value)
    root_relation = _branch_text_field(
        value,
        (
            "root_relation",
            "relation_to_root",
            "root_relationship",
            "relationship_to_root",
            "branch_root_relation",
        ),
    )
    if reason:
        row["reason"] = reason
    if root_relation:
        row["root_relation"] = root_relation


def _extract_branch_details(
    raw_group: Any,
    *,
    candidates: list[dict[str, Any]],
    by_candidate_id: dict[str, EntityNode],
) -> dict[str, dict[str, str]]:
    if not isinstance(raw_group, dict):
        return {}

    title_to_candidate_id, node_id_to_candidate_id = _candidate_lookup_maps(candidates)
    containers: list[Any] = []
    for key in (
        "selected_branches",
        "branches",
        "selected_branch_details",
        "selected_branch_reasons",
        "branch_reasons",
        "candidate_reasons",
        "depth_1_branch_reasons",
        "selected_candidate_reasons",
    ):
        value = raw_group.get(key)
        if value:
            containers.append(value)

    details: dict[str, dict[str, str]] = {}
    for container in containers:
        if isinstance(container, dict):
            for reference, value in container.items():
                candidate_id = _candidate_id_from_reference(
                    reference,
                    by_candidate_id=by_candidate_id,
                    title_to_candidate_id=title_to_candidate_id,
                    node_id_to_candidate_id=node_id_to_candidate_id,
                )
                if not candidate_id and isinstance(value, dict):
                    candidate_id = _candidate_id_from_reference(
                        value,
                        by_candidate_id=by_candidate_id,
                        title_to_candidate_id=title_to_candidate_id,
                        node_id_to_candidate_id=node_id_to_candidate_id,
                    )
                _merge_branch_detail(details, candidate_id, value)
        elif isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                candidate_id = _candidate_id_from_reference(
                    item,
                    by_candidate_id=by_candidate_id,
                    title_to_candidate_id=title_to_candidate_id,
                    node_id_to_candidate_id=node_id_to_candidate_id,
                )
                _merge_branch_detail(details, candidate_id, item)
    return details


def _normalize_group_ids(
    raw_ids: list[str],
    *,
    candidates: list[dict[str, Any]],
    by_candidate_id: dict[str, EntityNode],
    usage: Counter[str],
    group_size: int,
) -> list[str]:
    title_to_candidate_id, _node_id_to_candidate_id = _candidate_lookup_maps(candidates)
    candidate_ids = [str(candidate.get("candidate_id", "")).strip() for candidate in candidates]
    selected: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        candidate_id = str(raw_id).strip()
        if candidate_id not in by_candidate_id:
            candidate_id = title_to_candidate_id.get(candidate_id, "")
        if not candidate_id or candidate_id in seen or candidate_id not in by_candidate_id:
            continue
        selected.append(candidate_id)
        seen.add(candidate_id)
        if len(selected) >= group_size:
            break

    if len(selected) < group_size:
        remainder = sorted(
            [candidate_id for candidate_id in candidate_ids if candidate_id not in seen],
            key=lambda candidate_id: (usage[candidate_id], candidate_id),
        )
        selected.extend(remainder[: max(0, group_size - len(selected))])
    return _dedupe_homogeneous_group(
        selected[:group_size],
        candidates=candidates,
        usage=usage,
        group_size=group_size,
    )


def _parse_llm_groups(
    obj: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    by_candidate_id: dict[str, EntityNode],
    target_subgraphs: int,
    group_size: int,
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    raw_groups = obj.get("subgraphs")
    if not isinstance(raw_groups, list):
        raw_groups = obj.get("selected_subgraphs")
    if not isinstance(raw_groups, list):
        raw_groups = obj.get("groups")
    if not isinstance(raw_groups, list):
        raw_groups = []

    usage: Counter[str] = Counter()
    groups: list[list[str]] = []
    notes: list[dict[str, Any]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for raw_group in raw_groups:
        normalized = _normalize_group_ids(
            _raw_group_ids(raw_group),
            candidates=candidates,
            by_candidate_id=by_candidate_id,
            usage=usage,
            group_size=group_size,
        )
        if not normalized:
            continue
        key = tuple(sorted(normalized))
        if key in seen_groups:
            continue
        seen_groups.add(key)
        groups.append(normalized)
        usage.update(normalized)
        notes.append(
            {
                "selected_candidate_ids": list(normalized),
                "llm_selected_candidate_ids": list(normalized),
            }
        )
        if len(groups) >= target_subgraphs:
            break
    return groups, notes


def _fallback_groups(
    depth1_nodes: list[EntityNode],
    *,
    target_subgraphs: int,
    group_size: int,
    random_seed: int,
    existing_groups: list[list[str]] | None = None,
) -> list[list[str]]:
    if not depth1_nodes or target_subgraphs <= 0 or group_size <= 0:
        return []

    rng = random.Random(random_seed)
    candidate_ids = [f"d1_{idx:03d}" for idx, _node in enumerate(depth1_nodes, start=1)]
    candidate_payloads = [
        {"candidate_id": candidate_id, "wikipedia_title": node.wikipedia_title}
        for candidate_id, node in zip(candidate_ids, depth1_nodes)
    ]
    usage: Counter[str] = Counter()
    groups: list[list[str]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for group in existing_groups or []:
        clean_group = [candidate_id for candidate_id in group if candidate_id in candidate_ids]
        if not clean_group:
            continue
        clean_group = _dedupe_homogeneous_group(
            clean_group,
            candidates=candidate_payloads,
            usage=usage,
            group_size=group_size,
        )
        key = tuple(sorted(clean_group))
        if key in seen_groups:
            continue
        seen_groups.add(key)
        groups.append(clean_group)
        usage.update(clean_group)

    max_unique_groups = 1 if len(candidate_ids) <= group_size else max(target_subgraphs, len(candidate_ids) * 2)
    attempts = 0
    while len(groups) < target_subgraphs and attempts < max_unique_groups:
        attempts += 1
        shuffled = list(candidate_ids)
        rng.shuffle(shuffled)
        random_rank = {candidate_id: idx for idx, candidate_id in enumerate(shuffled)}
        shuffled.sort(key=lambda candidate_id: (usage[candidate_id], random_rank[candidate_id]))
        group = shuffled[: min(group_size, len(shuffled))]
        group = _dedupe_homogeneous_group(
            group,
            candidates=candidate_payloads,
            usage=usage,
            group_size=group_size,
        )
        key = tuple(sorted(group))
        if key in seen_groups and len(candidate_ids) > group_size:
            group = rng.sample(candidate_ids, k=min(group_size, len(candidate_ids)))
            group = _dedupe_homogeneous_group(
                group,
                candidates=candidate_payloads,
                usage=usage,
                group_size=group_size,
            )
            key = tuple(sorted(group))
        if key in seen_groups:
            continue
        seen_groups.add(key)
        groups.append(group)
        usage.update(group)
    return groups


def _select_depth1_groups_llm(
    graph: GraphData,
    root: EntityNode,
    depth1_nodes: list[EntityNode],
    cfg: Depth1BranchSamplingConfig,
) -> tuple[list[list[EntityNode]], list[dict[str, Any]], str]:
    group_size = min(max(1, int(cfg.depth1_branches_per_subgraph)), len(depth1_nodes))
    if group_size <= 0:
        return [], [], "no_depth1_nodes"

    children_by_id = _children_by_source_id(graph)
    candidates, by_candidate_id = _candidate_payloads(graph, root, depth1_nodes, children_by_id)
    llm_settings = cfg.llm_settings or {}
    fallback_reason = ""
    parsed_notes: list[dict[str, Any]] = []
    groups_by_candidate_id: list[list[str]] = []

    if str(cfg.selection_mode or "llm").strip().lower() == "llm":
        try:
            llm_cfg = LLMConfig.from_settings(llm_settings)
            client = OpenAIChatClient(llm_cfg)
            prompt_spec = load_prompt_spec(DEPTH1_SELECTION_PROMPT_FILE)
            user_prompt = render_user_prompt(
                DEPTH1_SELECTION_PROMPT_FILE,
                seed_entity=graph.metadata.seed_entity,
                root_summary=safe_snippet(root.text_snippet or " ".join(root.text[:2]), max_chars=900),
                target_subgraphs=int(cfg.target_subgraphs),
                depth_1_branches_per_subgraph=int(cfg.depth1_branches_per_subgraph),
                candidate_count=len(candidates),
                depth_1_candidates=candidates,
            )
            messages = [
                {"role": "system", "content": prompt_spec.system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            retried_after_bad_cache = False
            while True:
                raw, meta = client.chat(
                    messages,
                    task_type="subgraph_depth1_branch_selection",
                    call_context={
                        "seed_entity": graph.metadata.seed_entity,
                        "graph_id": graph.metadata.graph_id,
                        "candidate_count": len(candidates),
                        "target_subgraphs": int(cfg.target_subgraphs),
                        "depth_1_branches_per_subgraph": int(cfg.depth1_branches_per_subgraph),
                    },
                    return_meta=True,
                    attempt_hook=cfg.attempt_hook,
                )
                record_path = str(meta.get("record_path", ""))
                cache_path = str(meta.get("cache_path", ""))
                cache_hit = bool(meta.get("cache_hit", False))
                obj: dict[str, Any] | None = None
                try:
                    obj = parse_first_json_object(raw)
                    groups_by_candidate_id, parsed_notes = _parse_llm_groups(
                        obj,
                        candidates=candidates,
                        by_candidate_id=by_candidate_id,
                        target_subgraphs=int(cfg.target_subgraphs),
                        group_size=group_size,
                    )
                    if not groups_by_candidate_id:
                        raise ValueError("LLM returned no usable depth-1 branch groups.")
                except Exception as exc:
                    client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=str(exc))
                    invalidate_cache_path(cache_path)
                    if cache_hit and not retried_after_bad_cache:
                        retried_after_bad_cache = True
                        continue
                    raise
                client.annotate_record(record_path=record_path, parsed_object=obj, parse_error=None)
                break
        except Exception as exc:
            fallback_reason = f"llm_selection_fallback: {exc}"

    groups_by_candidate_id = _fallback_groups(
        depth1_nodes,
        target_subgraphs=int(cfg.target_subgraphs),
        group_size=group_size,
        random_seed=int(cfg.random_seed),
        existing_groups=groups_by_candidate_id,
    )
    groups = [[by_candidate_id[candidate_id] for candidate_id in group if candidate_id in by_candidate_id] for group in groups_by_candidate_id]
    notes: list[dict[str, Any]] = []
    for idx, _group in enumerate(groups):
        note = parsed_notes[idx] if idx < len(parsed_notes) else {}
        note = dict(note)
        selected_candidate_ids = list(groups_by_candidate_id[idx]) if idx < len(groups_by_candidate_id) else []
        note["selected_candidate_ids"] = selected_candidate_ids
        note.setdefault("llm_selected_candidate_ids", selected_candidate_ids)
        note["llm_branch_selection_reasons_by_candidate_id"] = {}
        note["llm_branch_root_relations_by_candidate_id"] = {}
        note.setdefault("selection_mode", str(cfg.selection_mode or "llm").strip().lower())
        if fallback_reason:
            note["fallback_reason"] = fallback_reason
        notes.append(note)
    return groups, notes, fallback_reason


def build_depth1_branch_sampling_artifacts(
    graph_payload: dict[str, Any],
    cfg: Depth1BranchSamplingConfig,
) -> SamplingArtifacts:
    """Sample depth-1 branch-tree subgraphs from one compact raw graph."""
    graph = _load_graph(graph_payload)
    root = _root_node(graph)
    depth1_nodes = _depth1_nodes(graph, root)
    if not depth1_nodes:
        raise ValueError(f"No depth-1 nodes found under root '{root.wikipedia_title}'")

    selected_groups, selection_notes, fallback_reason = _select_depth1_groups_llm(graph, root, depth1_nodes, cfg)
    selected_subgraphs = [
        _build_depth1_branch_subgraph(graph, root, group, selection_notes[idx] if idx < len(selection_notes) else {})
        for idx, group in enumerate(selected_groups)
        if group
    ]
    _renumber_selected_subgraphs(selected_subgraphs)
    query_specs = [subgraph.query_spec for subgraph in selected_subgraphs if subgraph.query_spec is not None]

    selected_node_titles: set[str] = set()
    selected_edge_ids: set[str] = set()
    selected_depth1_ids: set[str] = set()
    for subgraph in selected_subgraphs:
        selected_node_titles.update(subgraph.node_titles)
        selected_depth1_ids.update(subgraph.depth_1_node_ids)
        selected_edge_ids.update(edge.edge_id for edge in subgraph.edges)

    metadata = SamplingMetadata(
        artifact_type="depth1_branch_tree_sampling",
        graph_id=graph.metadata.graph_id,
        seed_entity=graph.metadata.seed_entity,
        relation_mode=graph.metadata.relation_mode,
        source_graph_path="",
        build_time_utc=utc_now_iso(),
        candidate_count=len(selected_subgraphs),
        selected_count=len(selected_subgraphs),
        candidate_counts_by_type={"depth1_branch_tree": len(selected_subgraphs)},
        selected_counts_by_type={"depth1_branch_tree": len(selected_subgraphs)},
        config=cfg.to_dict(),
        summary={
            "root_title": root.wikipedia_title,
            "depth_1_nodes_total": len(depth1_nodes),
            "target_subgraphs": int(cfg.target_subgraphs),
            "selected_subgraphs": len(selected_subgraphs),
            "depth_1_branches_per_subgraph": int(cfg.depth1_branches_per_subgraph),
            "paths_per_subgraph": int(cfg.depth1_branches_per_subgraph),
            "selection_mode": str(cfg.selection_mode or "llm").strip().lower(),
            "selection_fallback_reason": fallback_reason,
            "selected_depth_1_coverage_ratio": round(len(selected_depth1_ids) / max(1, len(depth1_nodes)), 4),
            "selected_node_coverage_ratio": round(len(selected_node_titles) / max(1, len(graph.entities)), 4),
            "selected_edge_coverage_ratio": round(len(selected_edge_ids) / max(1, len(graph.edges)), 4),
        },
    )
    return SamplingArtifacts(
        metadata=metadata,
        candidates=list(selected_subgraphs),
        selected_subgraphs=selected_subgraphs,
        query_specs=query_specs,
    )


def _renumber_selected_subgraphs(selected_subgraphs: list[SampledSubgraph]) -> None:
    """Rewrite subgraph ids to a compact ordered sequence."""
    total = len(selected_subgraphs)
    for idx, subgraph in enumerate(selected_subgraphs, start=1):
        old_id = subgraph.subgraph_id
        new_id = make_ordered_subgraph_id(idx, total=total)
        subgraph.subgraph_id = new_id
        subgraph.selection_notes = dict(subgraph.selection_notes or {})
        subgraph.selection_notes["subgraph_index"] = idx
        if old_id:
            subgraph.selection_notes["legacy_subgraph_id"] = old_id
        subgraph.template_metadata = dict(subgraph.template_metadata or {})
        subgraph.template_metadata["subgraph_index"] = idx
        if old_id:
            subgraph.template_metadata["legacy_subgraph_id"] = old_id
        if subgraph.query_spec is not None:
            subgraph.query_spec.subgraph_id = new_id


# Alternate import names for the depth-1 branch sampling configuration and builder.
LeafPathSamplingConfig = Depth1BranchSamplingConfig
build_leaf_path_sampling_artifacts = build_depth1_branch_sampling_artifacts
