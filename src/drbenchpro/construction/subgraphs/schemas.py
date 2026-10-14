"""Schemas for programmatic subgraph sampling artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from drbenchpro.construction.graph.schemas import RealizedNeighborLink


@dataclass
class SubgraphEdgeView:
    """Compact edge payload stored inside sampled subgraph artifacts."""

    edge_id: str
    source_title: str
    target_title: str
    edge_depth: int | None = None
    relation_label: str = ""
    relation_explanation: str = ""
    anchor_text: str = ""


@dataclass
class SubgraphNodeMaterial:
    """Node-level textual material kept for later query generation."""

    wikipedia_title: str
    node_id: str = ""
    url: str = ""
    text_snippet: str = ""
    text: list[str] = field(default_factory=list)
    root_depth: int | None = None
    realized_out_neighbors: list[RealizedNeighborLink] = field(default_factory=list)


@dataclass
class SubgraphEdgeMaterial:
    """Rich edge material kept for later query generation."""

    edge_id: str
    source_title: str
    target_title: str
    edge_depth: int | None = None
    anchor_text: str = ""
    paragraph_id: int = -1
    context_text: str = ""
    source_url: str = ""
    target_url: str = ""
    href: str = ""
    source_text_snippet: str = ""
    target_text_snippet: str = ""
    relation_label: str = ""
    relation_explanation: str = ""


@dataclass
class QueryMaterials:
    """Bundled node/edge material for later LLM query synthesis."""

    nodes: list[SubgraphNodeMaterial] = field(default_factory=list)
    edges: list[SubgraphEdgeMaterial] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuerySpec:
    """Structured query specification for later natural-language generation."""

    subgraph_id: str
    subgraph_type: str
    anchor_topic: str
    focus_nodes: list[str] = field(default_factory=list)
    focus_edges: list[dict[str, Any]] = field(default_factory=list)
    depth_1_node_titles: list[str] = field(default_factory=list)
    depth_1_node_ids: list[str] = field(default_factory=list)
    excluded_nodes: list[str] = field(default_factory=list)
    intended_user_intent: str = ""
    desired_answer_scope: dict[str, Any] = field(default_factory=dict)
    breadth_depth_profile: dict[str, Any] = field(default_factory=dict)
    graph_stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class SampledSubgraph:
    """One candidate or selected subgraph sampled from a large wiki graph."""

    subgraph_id: str
    subgraph_type: str
    anchor_node: str
    node_titles: list[str] = field(default_factory=list)
    depth_1_node_titles: list[str] = field(default_factory=list)
    depth_1_node_ids: list[str] = field(default_factory=list)
    depth_1_branch_selection_reasons: list[dict[str, str]] = field(default_factory=list)
    edges: list[SubgraphEdgeView] = field(default_factory=list)
    layer_map: dict[str, int] = field(default_factory=dict)
    main_path: list[str] = field(default_factory=list)
    template_metadata: dict[str, Any] = field(default_factory=dict)
    graph_stats: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    normalized_scores: dict[str, float] = field(default_factory=dict)
    filter_reasons: list[str] = field(default_factory=list)
    selection_status: str = "candidate"
    selection_notes: dict[str, Any] = field(default_factory=dict)
    query_spec: QuerySpec | None = None
    query_text_placeholder: str | None = None
    query_materials: QueryMaterials | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return asdict(self)


@dataclass
class SamplingMetadata:
    """Metadata for one subgraph sampling run."""

    artifact_type: str
    graph_id: str
    seed_entity: str
    relation_mode: str
    source_graph_path: str
    build_time_utc: str
    candidate_count: int
    selected_count: int
    candidate_counts_by_type: dict[str, int] = field(default_factory=dict)
    selected_counts_by_type: dict[str, int] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class SamplingArtifacts:
    """Top-level bundle returned by the subgraph sampling pipeline."""

    metadata: SamplingMetadata
    candidates: list[SampledSubgraph] = field(default_factory=list)
    selected_subgraphs: list[SampledSubgraph] = field(default_factory=list)
    query_specs: list[QuerySpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return asdict(self)
