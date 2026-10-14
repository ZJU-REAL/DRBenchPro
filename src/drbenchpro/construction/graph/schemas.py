"""Schemas for local Wikipedia graph artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from drbenchpro.common.ids import make_edge_id, make_ordered_edge_id


@dataclass
class AnchorEvidence:
    """Anchor-level evidence extracted from one paragraph link mention."""

    anchor_id: str
    wikipedia_title: str
    href: str
    text: str
    paragraph_id: int
    start: int
    end: int
    context_text: str
    source_title: str
    source_url: str
    target_url: str
    wikipedia_id: str


@dataclass
class RealizedNeighborLink:
    """Actual graph out-link retained in the built graph, aggregated by target."""

    target_title: str
    edge_ids: list[str] = field(default_factory=list)
    anchor_texts: list[str] = field(default_factory=list)
    paragraph_ids: list[int] = field(default_factory=list)
    target_url: str = ""
    target_text_snippet: str = ""


@dataclass
class EntityNode:
    """Entity node with intro text and outbound anchor evidence."""

    _id: str
    wikipedia_id: str
    wikipedia_title: str
    url: str
    depth: int = 0
    source_id: str = ""
    source_title: str = ""
    text: list[str] = field(default_factory=list)
    text_snippet: str = ""
    anchors: list[AnchorEvidence] = field(default_factory=list)
    realized_out_neighbors: list[RealizedNeighborLink] = field(default_factory=list)


@dataclass
class EdgeRecord:
    """Edge record grounded in one anchor evidence."""

    edge_id: str
    source_title: str
    target_title: str
    anchor_text: str
    paragraph_id: int
    start: int
    end: int
    context_text: str
    source_url: str
    target_url: str
    href: str
    target_text_snippet: str = ""
    relation_label: str | None = None
    relation_explanation: str | None = None
    relation_mode: str = "none"


@dataclass
class GraphMetadata:
    """Graph-level metadata."""

    graph_id: str
    seed_entity: str
    root_url: str
    language: str
    max_nodes: int
    max_edges: int
    max_depth: int
    max_neighbors_per_node: int
    root_max_neighbors_per_node: int
    build_time_utc: str
    relation_mode: str
    root_expand_all_intro_links: bool = False
    root_link_selection_mode: str = "intro"
    root_full_page_prompt_file: str = "graph_root_full_page_selection.md"
    root_full_page_llm_target_neighbors: int = 30
    neighbor_selection_mode: str = "heuristic"
    llm_candidate_pool_limit: int = 0
    root_llm_candidate_pool_limit: int = 0
    page_fetch_concurrency: int = 1
    page_fetch_attempt_stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphData:
    """Complete graph artifact."""

    metadata: GraphMetadata
    entities: list[EntityNode] = field(default_factory=list)
    edges: list[EdgeRecord] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert dataclass graph object into JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GraphData":
        """Build a GraphData object from a plain dictionary."""
        meta_raw = payload.get("metadata", {})
        metadata = GraphMetadata(
            graph_id=meta_raw.get("graph_id", ""),
            seed_entity=meta_raw.get("seed_entity", ""),
            root_url=meta_raw.get("root_url", ""),
            language=meta_raw.get("language", "en"),
            max_nodes=int(meta_raw.get("max_nodes", 0)),
            max_edges=int(meta_raw.get("max_edges", max(int(meta_raw.get("max_nodes", 0)) - 1, 0))),
            max_depth=int(meta_raw.get("max_depth", 0)),
            max_neighbors_per_node=int(meta_raw.get("max_neighbors_per_node", 0)),
            root_max_neighbors_per_node=int(
                meta_raw.get("root_max_neighbors_per_node", meta_raw.get("max_neighbors_per_node", 0))
            ),
            root_expand_all_intro_links=bool(meta_raw.get("root_expand_all_intro_links", False)),
            root_link_selection_mode=str(meta_raw.get("root_link_selection_mode", "intro") or "intro"),
            root_full_page_prompt_file=str(
                meta_raw.get("root_full_page_prompt_file", "graph_root_full_page_selection.md") or "graph_root_full_page_selection.md"
            ),
            root_full_page_llm_target_neighbors=int(meta_raw.get("root_full_page_llm_target_neighbors", 30) or 30),
            neighbor_selection_mode=str(meta_raw.get("neighbor_selection_mode", "heuristic") or "heuristic"),
            llm_candidate_pool_limit=int(meta_raw.get("llm_candidate_pool_limit", 0) or 0),
            root_llm_candidate_pool_limit=int(
                meta_raw.get("root_llm_candidate_pool_limit", meta_raw.get("llm_candidate_pool_limit", 0)) or 0
            ),
            page_fetch_concurrency=int(meta_raw.get("page_fetch_concurrency", 1) or 1),
            page_fetch_attempt_stats=dict(meta_raw.get("page_fetch_attempt_stats", {}) or {}),
            build_time_utc=meta_raw.get("build_time_utc", ""),
            relation_mode=meta_raw.get("relation_mode", "none"),
        )

        entities: list[EntityNode] = []
        entity_id_by_title: dict[str, str] = {}
        compact_neighbors_by_title: dict[str, dict[str, list[str]]] = {}
        for item in payload.get("entities", []):
            anchors = [
                AnchorEvidence(
                    anchor_id=a.get("anchor_id", ""),
                    wikipedia_title=a.get("wikipedia_title", ""),
                    href=a.get("href", ""),
                    text=a.get("text", ""),
                    paragraph_id=int(a.get("paragraph_id", -1)),
                    start=int(a.get("start", -1)),
                    end=int(a.get("end", -1)),
                    context_text=a.get("context_text", ""),
                    source_title=a.get("source_title", ""),
                    source_url=a.get("source_url", ""),
                    target_url=a.get("target_url", ""),
                    wikipedia_id=a.get("wikipedia_id", ""),
                )
                for a in item.get("anchors", [])
            ]
            realized_out_neighbors = [
                RealizedNeighborLink(
                    target_title=neighbor.get("target_title", ""),
                    edge_ids=list(neighbor.get("edge_ids", [])),
                    anchor_texts=list(neighbor.get("anchor_texts", [])),
                    paragraph_ids=[int(v) for v in neighbor.get("paragraph_ids", [])],
                    target_url=neighbor.get("target_url", ""),
                    target_text_snippet=neighbor.get("target_text_snippet", ""),
                )
                for neighbor in item.get("realized_out_neighbors", [])
            ]
            out_neighbor_titles = [str(v) for v in item.get("out_neighbor_titles", []) if str(v).strip()]
            out_neighbor_ids = [str(v) for v in item.get("out_neighbor_ids", [])]
            out_edge_ids = [str(v) for v in item.get("out_edge_ids", [])]
            if out_neighbor_titles and not realized_out_neighbors:
                realized_out_neighbors = [
                    RealizedNeighborLink(
                        target_title=title,
                        edge_ids=[out_edge_ids[idx]] if idx < len(out_edge_ids) and str(out_edge_ids[idx]).strip() else [],
                    )
                    for idx, title in enumerate(out_neighbor_titles)
                ]
            entities.append(
                EntityNode(
                    _id=item.get("_id", ""),
                    wikipedia_id=item.get("wikipedia_id", ""),
                    wikipedia_title=item.get("wikipedia_title", ""),
                    depth=int(item.get("depth", 0)),
                    source_id=item.get("source_id", ""),
                    source_title=item.get("source_title", ""),
                    url=item.get("url", ""),
                    text=list(item.get("text", [])),
                    text_snippet=item.get("text_snippet", ""),
                    anchors=anchors,
                    realized_out_neighbors=realized_out_neighbors,
                )
            )
            title = str(item.get("wikipedia_title", "")).strip()
            entity_id_by_title[title] = str(item.get("_id", ""))
            compact_neighbors_by_title[title] = {
                "out_neighbor_titles": out_neighbor_titles,
                "out_neighbor_ids": out_neighbor_ids,
                "out_edge_ids": out_edge_ids,
            }

        edges: list[EdgeRecord] = []
        raw_edges = payload.get("edges", [])
        if not raw_edges:
            synthetic_index = 0
            for entity in entities:
                source_title = entity.wikipedia_title
                compact_neighbors = compact_neighbors_by_title.get(source_title, {})
                out_titles = list(compact_neighbors.get("out_neighbor_titles", []))
                out_edge_ids = list(compact_neighbors.get("out_edge_ids", []))
                for idx, target_title in enumerate(out_titles):
                    synthetic_index += 1
                    cached_edge_id = str(out_edge_ids[idx]).strip() if idx < len(out_edge_ids) else ""
                    edge_id = cached_edge_id or make_ordered_edge_id(
                        synthetic_index,
                        total=int(meta_raw.get("edge_count", 0)) or None,
                    )
                    edges.append(
                        EdgeRecord(
                            edge_id=edge_id,
                            source_title=source_title,
                            target_title=target_title,
                            anchor_text="",
                            paragraph_id=-1,
                            start=-1,
                            end=-1,
                            context_text="",
                            source_url="",
                            target_url="",
                            href="",
                            target_text_snippet="",
                            relation_label=None,
                            relation_explanation=None,
                            relation_mode="none",
                        )
                    )
        for item in raw_edges:
            edges.append(
                EdgeRecord(
                    edge_id=item.get("edge_id", ""),
                    source_title=item.get("source_title", ""),
                    target_title=item.get("target_title", ""),
                    anchor_text=item.get("anchor_text", ""),
                    paragraph_id=int(item.get("paragraph_id", -1)),
                    start=int(item.get("start", -1)),
                    end=int(item.get("end", -1)),
                    context_text=item.get("context_text", ""),
                    source_url=item.get("source_url", ""),
                    target_url=item.get("target_url", ""),
                    href=item.get("href", ""),
                    target_text_snippet=item.get("target_text_snippet", ""),
                    relation_label=item.get("relation_label"),
                    relation_explanation=item.get("relation_explanation"),
                    relation_mode=item.get("relation_mode", "none"),
                )
            )
        return cls(
            metadata=metadata,
            entities=entities,
            edges=edges,
            diagnostics=dict(payload.get("diagnostics", {}) or {}),
        )
