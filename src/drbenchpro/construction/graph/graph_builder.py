"""Seed-expansion local graph builder using Wikipedia anchors."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable

from .neighbor_selector import (
    _is_near_duplicate_title,
    _title_similarity_tokens,
    select_neighbors_llm,
    select_root_full_page_neighbors_llm,
)
from .relation_generator import generate_relation, generate_relations_batch
from .schemas import AnchorEvidence, EdgeRecord, EntityNode, GraphData, GraphMetadata
from .wikipedia_client import WikipediaClient, WikipediaRequestError
from .wikipedia_parser import ParsedAnchor, ParsedEntityPage, WikipediaParser
from drbenchpro.common.build_reporter import GraphBuildReporter, ReporterConfig
from drbenchpro.common.ids import make_graph_id, make_ordered_edge_id
from drbenchpro.common.io import utc_now_iso
from drbenchpro.common.text import safe_snippet


class GraphBuildError(RuntimeError):
    """Raised when graph construction fails at root level."""


@dataclass
class GraphBuildConfig:
    """Configuration for local graph construction."""

    language: str = "en"
    max_nodes: int = 20
    max_edges: int | None = None
    max_depth: int | None = None
    max_neighbors_per_node: int = 3
    root_max_neighbors_per_node: int | None = None
    root_expand_all_intro_links: bool = False
    root_link_selection_mode: str = "intro"
    root_full_page_prompt_file: str = "graph_root_full_page_selection.md"
    root_full_page_llm_target_neighbors: int = 30
    neighbor_selection_mode: str = "heuristic"
    llm_candidate_pool_limit: int = 64
    root_llm_candidate_pool_limit: int | None = None
    page_fetch_concurrency: int = 8
    sleep_seconds: float = 0.2
    relation_mode: str = "none"
    relation_llm_settings: dict | None = None
    neighbor_selection_llm_settings: dict | None = None
    show_progress: bool = False
    progress_log_file: str | None = None
    progress_jsonl_file: str | None = None
    progress_command: str | None = None
    progress_hook: Callable[[dict[str, Any]], None] | None = None


class GraphBuilder:
    """Build a local knowledge graph with seed-driven neighbor expansion."""

    def __init__(self, client: WikipediaClient, parser: WikipediaParser, config: GraphBuildConfig):
        self.client = client
        self.parser = parser
        self.config = config
        self._node_counter = 0
        self._edge_counter = 0
        self._page_fetch_lock = Lock()
        self._page_fetch_attempt_records: list[dict[str, Any]] = []
        self._active_reporter: GraphBuildReporter | None = None

    def _next_node_id(self) -> str:
        """Allocate a stable BFS-order node id."""
        self._node_counter += 1
        return f"node_{self._node_counter:06d}"

    def _next_edge_id(self) -> str:
        """Allocate a stable BFS-order edge id."""
        self._edge_counter += 1
        return make_ordered_edge_id(self._edge_counter, total=self._resolve_max_edges())

    def _emit_progress(self, stage: str, **fields: Any) -> None:
        """Emit lightweight structured progress updates to an external hook."""
        if self.config.progress_hook is None:
            return
        payload = {"stage": stage, **fields}
        self.config.progress_hook(payload)

    def _resolve_max_edges(self) -> int:
        """
        Resolve effective edge cap for current run.

        Default behavior follows tree-like expansion: max_edges = max_nodes - 1.
        """
        if self.config.max_edges is not None:
            return max(0, int(self.config.max_edges))
        return max(0, int(self.config.max_nodes) - 1)

    def build_graph(self, root_title: str | None = None, random_root: bool = False) -> GraphData:
        """
        Build graph from explicit root title or random Wikipedia root.

        Raises:
            GraphBuildError: when root cannot be fetched or parsed.
        """
        resolved_max_edges = self._resolve_max_edges()
        planned_llm_calls = 1 if self.config.relation_mode == "llm_batch" else (resolved_max_edges if self.config.relation_mode == "llm" else 0)

        reporter = GraphBuildReporter(
            ReporterConfig(
                enabled_console=self.config.show_progress,
                max_nodes=self.config.max_nodes,
                edge_total=max(1, resolved_max_edges),
                relation_mode=self.config.relation_mode,
                llm_total=planned_llm_calls,
                text_log_path=self.config.progress_log_file,
                jsonl_log_path=self.config.progress_jsonl_file,
                command_line=self.config.progress_command,
            )
        )
        reporter.start()
        self._active_reporter = reporter
        build_t0 = time.perf_counter()
        self._reset_page_fetch_attempt_records()
        reporter.log(
            "build_start",
            "Starting graph build",
            seed=root_title or "",
            random_root=random_root,
            max_nodes=self.config.max_nodes,
            max_edges=resolved_max_edges,
            max_neighbors=self.config.max_neighbors_per_node,
            root_max_neighbors=(
                int(self.config.root_max_neighbors_per_node)
                if self.config.root_max_neighbors_per_node is not None
                else int(self.config.max_neighbors_per_node)
            ),
                root_expand_all_intro_links=bool(self.config.root_expand_all_intro_links),
                root_link_selection_mode=str(self.config.root_link_selection_mode or "intro"),
                root_full_page_prompt_file=str(self.config.root_full_page_prompt_file or "graph_root_full_page_selection.md"),
                root_full_page_llm_target_neighbors=int(self.config.root_full_page_llm_target_neighbors),
            neighbor_selection_mode=self.config.neighbor_selection_mode,
            llm_candidate_pool_limit=int(self.config.llm_candidate_pool_limit),
            root_llm_candidate_pool_limit=self._root_llm_candidate_pool_limit(),
            page_fetch_concurrency=int(self.config.page_fetch_concurrency),
            relation_mode=self.config.relation_mode,
        )

        def emit_attempt_progress(event: dict[str, object]) -> None:
            if str(event.get("status")) != "error":
                return
            error = event.get("error", {}) or {}
            if not isinstance(error, dict):
                error = {}
            retry_in = float(event.get("next_retry_in_seconds", 0.0) or 0.0)
            reporter.log(
                "llm_attempt_failed",
                "LLM attempt failed",
                level="warn",
                phase=str(event.get("phase", "")).strip(),
                attempt=int(event.get("attempt", 0) or 0),
                attempts_total=int(event.get("attempts_total", 0) or 0),
                elapsed_ms=int(event.get("elapsed_ms", 0) or 0),
                status_code=error.get("status_code"),
                error_type=error.get("error_type"),
                retry_in=(f"{retry_in:.1f}s" if retry_in > 0 else "no_more_retries"),
                source=str(event.get("source_title", "")).strip(),
                target=str(event.get("target_title", "")).strip(),
                edge_count=int(event.get("edge_count", 0) or 0),
                candidate_count=int(event.get("candidate_count", 0) or 0),
                target_count=int(event.get("target_count", 0) or 0),
                detail=str(error.get("detail", "")),
            )
            if retry_in <= 0:
                reporter.log(
                    "llm_terminal_failure",
                    "LLM call exhausted retries",
                    level="error",
                    phase=str(event.get("phase", "")).strip(),
                    attempt=int(event.get("attempt", 0) or 0),
                    attempts_total=int(event.get("attempts_total", 0) or 0),
                    elapsed_ms=int(event.get("elapsed_ms", 0) or 0),
                    status_code=error.get("status_code"),
                    error_type=error.get("error_type"),
                    source=str(event.get("source_title", "")).strip(),
                    target=str(event.get("target_title", "")).strip(),
                    edge_count=int(event.get("edge_count", 0) or 0),
                    candidate_count=int(event.get("candidate_count", 0) or 0),
                    target_count=int(event.get("target_count", 0) or 0),
                    detail=str(error.get("detail", "")),
                )

        try:
            if random_root:
                try:
                    root_title = self.client.get_random_title()
                    reporter.log("root_random", "Resolved random root", root_title=root_title)
                except WikipediaRequestError as exc:
                    reporter.log("root_error", "Failed to resolve random root", level="error", error=str(exc))
                    raise GraphBuildError(f"Failed to resolve random root: {exc}") from exc

            if not root_title:
                reporter.log("root_error", "Missing root title", level="error")
                raise GraphBuildError("Root title is required unless --random-root is used.")

            root_page = self._fetch_and_parse_page(
                root_title,
                supplement_anchor_target_count=(
                    None if self._use_intro_only_expansion() or self._use_root_full_page_llm_selection() else self._neighbor_limit_for_depth(0)
                ),
                include_supplemental_tables=False if self._use_root_full_page_llm_selection() else not self._use_intro_only_expansion(),
                collect_all_supplemental_anchors=self._use_root_full_page_llm_selection(),
                include_supplemental_lists=self._use_root_full_page_llm_selection(),
            )
            root_entity = self._to_entity_node(
                root_page,
                node_id=self._next_node_id(),
                depth=0,
                source_id="",
                source_title="",
            )
            entities_by_title: dict[str, EntityNode] = {root_entity.wikipedia_title: root_entity}
            current_frontier: list[str] = [root_entity.wikipedia_title]
            expanded: set[str] = set()
            edges: list[EdgeRecord] = []
            edge_keys: set[tuple[str, str, int, int, int]] = set()
            node_expansion_records: dict[str, dict[str, Any]] = {}
            reporter.log("root_loaded", "Fetched root page", title=root_entity.wikipedia_title, anchors=len(root_entity.anchors))
            self._emit_progress(
                "root_loaded",
                root_title=root_entity.wikipedia_title,
                depth=root_entity.depth,
                nodes=len(entities_by_title),
            )
            reporter.update_nodes(len(entities_by_title))

            while current_frontier and len(entities_by_title) <= self.config.max_nodes and len(edges) < resolved_max_edges:
                level_plans: list[dict[str, Any]] = []
                next_frontier: list[str] = []
                frontier_titles = [title for title in current_frontier if title in entities_by_title and title not in expanded]

                for source_title in frontier_titles:
                    source_entity = entities_by_title[source_title]
                    source_depth = int(source_entity.depth)
                    node_t0 = time.perf_counter()
                    if self.config.max_depth is not None and source_depth >= int(self.config.max_depth):
                        reporter.log(
                            "node_depth_cap_reached",
                            "Node reached max depth; skip further expansion",
                            source=source_title,
                            depth=source_depth,
                            max_depth=int(self.config.max_depth),
                        )
                        reporter.log(
                            "node_done",
                            "Finished node expansion",
                            source=source_title,
                            elapsed_ms=int((time.perf_counter() - node_t0) * 1000),
                            nodes=len(entities_by_title),
                            edges=len(edges),
                            queue=0,
                        )
                        self._emit_progress(
                            "node_done",
                            root_title=root_entity.wikipedia_title,
                            source_title=source_title,
                            depth=source_depth,
                            nodes=len(entities_by_title),
                        )
                        expanded.add(source_title)
                        continue

                    neighbor_limit = self._neighbor_limit_for_depth(source_depth)
                    candidates = self._candidate_anchors(source_entity, entities_by_title)
                    llm_candidates = self._truncate_candidates_for_llm(candidates, depth=source_depth)
                    use_llm = self._should_use_llm_neighbor_selection(source_depth, llm_candidates, neighbor_limit)
                    if self._should_use_root_full_page_llm_selection(source_depth) and candidates:
                        use_llm = True
                    if len(llm_candidates) < len(candidates):
                        reporter.log(
                            "llm_candidate_pool_truncated",
                            "Truncated llm neighbor candidate pool",
                            source=source_title,
                            depth=source_depth,
                            original_candidates=len(candidates),
                            llm_candidates=len(llm_candidates),
                            llm_candidate_pool_limit=self._llm_candidate_pool_limit_for_depth(source_depth),
                        )
                    level_plans.append(
                        {
                            "source_title": source_title,
                            "source_entity": source_entity,
                            "source_depth": source_depth,
                            "node_t0": node_t0,
                            "neighbor_limit": neighbor_limit,
                            "candidates": candidates,
                            "llm_candidates": llm_candidates,
                            "use_llm": use_llm,
                            "selected_raw": [],
                            "selected": [],
                        }
                    )

                llm_plan_count = sum(1 for plan in level_plans if bool(plan["use_llm"]))
                if llm_plan_count > 0:
                    planned_llm_calls += llm_plan_count
                    reporter.set_llm_total(planned_llm_calls)

                selection_results = self._select_neighbors_for_frontier(
                    level_plans=level_plans,
                    reporter=reporter,
                    emit_attempt_progress=emit_attempt_progress,
                )

                reserved_titles = set(entities_by_title.keys())
                resolve_tasks: list[tuple[str, AnchorEvidence, EntityNode]] = []
                for plan in level_plans:
                    source_title = str(plan["source_title"])
                    source_entity = plan["source_entity"]
                    source_depth = int(plan["source_depth"])
                    raw_selected = list(selection_results.get(source_title, []))
                    accepted_selected = self._accept_selected_neighbors(
                        selected=raw_selected,
                        candidates=list(plan["candidates"]),
                        reserved_titles=reserved_titles,
                        limit=int(plan["neighbor_limit"]),
                        expand_all=self._should_expand_all_root_links(source_depth),
                    )
                    plan["selected_raw"] = raw_selected
                    plan["selected"] = accepted_selected
                    node_expansion_records[source_title] = {
                        "source_id": source_entity._id,
                        "source_title": source_title,
                        "depth": source_depth,
                        "candidate_count": len(plan["candidates"]),
                        "llm_candidate_count": len(plan["llm_candidates"]),
                        "selected_raw_count": len(raw_selected),
                        "accepted_selected_count": len(accepted_selected),
                        "neighbor_limit": int(plan["neighbor_limit"]),
                        "used_llm": bool(plan["use_llm"]),
                        "selection_fallback": bool(plan.get("selection_fallback", False)),
                        "selection_error": str(plan.get("selection_error", "") or ""),
                        "selected_raw_titles": [
                            str(anchor.wikipedia_title).strip()
                            for anchor in raw_selected
                            if str(anchor.wikipedia_title).strip()
                        ],
                        "accepted_selected_titles": [
                            str(anchor.wikipedia_title).strip()
                            for anchor in accepted_selected
                            if str(anchor.wikipedia_title).strip()
                        ],
                    }
                    reporter.log(
                        "node_expand",
                        "Expanding node",
                        source=source_title,
                        candidates=len(plan["candidates"]),
                        llm_candidates=len(plan["llm_candidates"]),
                        selected_raw=len(raw_selected),
                        selected=len(accepted_selected),
                        neighbor_limit=("all_intro_links" if self._should_expand_all_root_links(source_depth) else int(plan["neighbor_limit"])),
                        nodes=len(entities_by_title),
                        edges=len(edges),
                        queue=max(0, len(frontier_titles) - len(expanded) - 1 + len(next_frontier)),
                    )
                    self._emit_progress(
                        "node_expand",
                        root_title=root_entity.wikipedia_title,
                        source_title=source_title,
                        depth=source_depth,
                        nodes=len(entities_by_title),
                    )
                    for anchor in accepted_selected:
                        resolve_tasks.append((source_title, anchor, source_entity))

                resolved_results = self._resolve_targets_for_frontier(resolve_tasks)

                stop_build = False
                queued_titles: set[str] = set()
                for plan in level_plans:
                    source_title = str(plan["source_title"])
                    source_entity = plan["source_entity"]
                    source_depth = int(plan["source_depth"])
                    for idx, anchor in enumerate(plan["selected"], start=1):
                        if len(edges) >= resolved_max_edges:
                            reporter.log("edge_cap_reached", "Edge cap reached, stop adding neighbors", source=source_title)
                            stop_build = True
                            break
                        if len(entities_by_title) >= self.config.max_nodes:
                            reporter.log("node_cap_reached", "Node cap reached, stop adding neighbors", source=source_title)
                            stop_build = True
                            break

                        resolved_key = (source_title, anchor.anchor_id)
                        resolved_result = resolved_results.get(resolved_key)
                        if resolved_result is None:
                            continue
                        target_entity, can_expand, resolve_ms = resolved_result
                        reporter.log(
                            "edge_candidate",
                            "Processing selected anchor",
                            source=source_title,
                            target_hint=anchor.wikipedia_title,
                            rank=idx,
                            anchor_text=anchor.text,
                        )
                        target_title = target_entity.wikipedia_title
                        source_title_now = source_entity.wikipedia_title

                        if target_title not in entities_by_title:
                            target_entity._id = self._next_node_id()
                            entities_by_title[target_title] = target_entity
                            reporter.update_nodes(len(entities_by_title))
                            reporter.log(
                                "node_discovered",
                                "Discovered new node",
                                source=source_title_now,
                                target=target_title,
                                target_id=target_entity._id,
                                depth=target_entity.depth,
                                anchors=len(target_entity.anchors),
                                resolve_ms=resolve_ms,
                            )
                            self._emit_progress(
                                "node_discovered",
                                root_title=root_entity.wikipedia_title,
                                source_title=source_title_now,
                                target_title=target_title,
                                depth=target_entity.depth,
                                nodes=len(entities_by_title),
                            )
                            if (
                                can_expand
                                and target_title not in expanded
                                and target_title not in queued_titles
                                and (
                                    self.config.max_depth is None
                                    or target_entity.depth < int(self.config.max_depth)
                                )
                            ):
                                next_frontier.append(target_title)
                                queued_titles.add(target_title)

                        edge_key = (
                            source_title_now,
                            target_title,
                            anchor.paragraph_id,
                            anchor.start,
                            anchor.end,
                        )
                        if edge_key in edge_keys:
                            reporter.log("edge_skip_duplicate", "Skipping duplicate edge evidence", source=source_title_now, target=target_title)
                            continue
                        edge_keys.add(edge_key)

                        edge_payload = {
                            "source_title": source_title_now,
                            "target_title": target_title,
                            "anchor_text": anchor.text,
                            "context_text": anchor.context_text,
                            "href": anchor.href,
                            "target_text_snippet": entities_by_title[target_title].text_snippet,
                        }
                        relation_t0 = time.perf_counter()
                        relation_label = None
                        relation_explanation = None
                        edge_relation_mode = self.config.relation_mode
                        if self.config.relation_mode == "llm":
                            reporter.log("llm_start", "Relation LLM call started", source=source_title_now, target=target_title)
                            try:
                                relation_label, relation_explanation = generate_relation(
                                    edge_payload,
                                    mode=self.config.relation_mode,
                                    llm_settings=self.config.relation_llm_settings,
                                    attempt_hook=emit_attempt_progress,
                                )
                            except Exception as exc:
                                relation_ms = int((time.perf_counter() - relation_t0) * 1000)
                                reporter.advance_llm()
                                reporter.log(
                                    "llm_error",
                                    "Relation generation failed; fallback applied",
                                    level="warn",
                                    source=source_title_now,
                                    target=target_title,
                                    elapsed_ms=relation_ms,
                                    error=str(exc),
                                )
                                relation_label = "other"
                                relation_explanation = f"fallback_on_error: {exc}"
                            else:
                                relation_ms = int((time.perf_counter() - relation_t0) * 1000)
                                reporter.advance_llm()
                                reporter.log(
                                    "llm_done",
                                    "Relation LLM call finished",
                                    level="ok",
                                    source=source_title_now,
                                    target=target_title,
                                    elapsed_ms=relation_ms,
                                    label=relation_label,
                                )
                        elif self.config.relation_mode in {"rule", "none"}:
                            relation_label, relation_explanation = generate_relation(
                                edge_payload,
                                mode=self.config.relation_mode,
                                llm_settings=self.config.relation_llm_settings,
                            )

                        root_relation_explanation = ""
                        if source_depth == 0 and self._use_root_full_page_llm_selection():
                            root_relation_explanation = str(
                                getattr(anchor, "root_relation_explanation", "") or ""
                            ).strip() or self._fallback_root_relation_explanation(
                                root_title=source_title_now,
                                target_title=target_title,
                                anchor=anchor,
                            )
                        if root_relation_explanation:
                            relation_explanation = root_relation_explanation
                            if not relation_label:
                                relation_label = "root_first_hop_relation"
                            if str(edge_relation_mode or "none").strip().lower() == "none":
                                edge_relation_mode = "root_full_page_llm"

                        edge_id = self._next_edge_id()
                        edges.append(
                            EdgeRecord(
                                edge_id=edge_id,
                                source_title=source_title_now,
                                target_title=target_title,
                                anchor_text=anchor.text,
                                paragraph_id=anchor.paragraph_id,
                                start=anchor.start,
                                end=anchor.end,
                                context_text=anchor.context_text,
                                source_url=source_entity.url,
                                target_url=entities_by_title[target_title].url,
                                href=anchor.href,
                                target_text_snippet=entities_by_title[target_title].text_snippet,
                                relation_label=relation_label,
                                relation_explanation=relation_explanation,
                                relation_mode=edge_relation_mode,
                            )
                        )
                        reporter.advance_edge()
                        reporter.log(
                            "edge_added",
                            "Added graph edge",
                            level="ok",
                            source=source_title_now,
                            target=target_title,
                            edge_id=edge_id,
                            nodes=len(entities_by_title),
                            edges=len(edges),
                            queue=len(next_frontier),
                        )
                        self._emit_progress(
                            "edge_added",
                            root_title=root_entity.wikipedia_title,
                            source_title=source_title_now,
                            target_title=target_title,
                            depth=source_depth,
                            nodes=len(entities_by_title),
                        )

                    node_ms = int((time.perf_counter() - float(plan["node_t0"])) * 1000)
                    reporter.log(
                        "node_done",
                        "Finished node expansion",
                        source=source_title,
                        elapsed_ms=node_ms,
                        nodes=len(entities_by_title),
                        edges=len(edges),
                        queue=len(next_frontier),
                    )
                    self._emit_progress(
                        "node_done",
                        root_title=root_entity.wikipedia_title,
                        source_title=source_title,
                        depth=source_depth,
                        nodes=len(entities_by_title),
                    )
                    expanded.add(source_title)
                    if stop_build:
                        break

                if stop_build:
                    break
                current_frontier = next_frontier

            if self.config.relation_mode == "llm_batch" and edges:
                batch_t0 = time.perf_counter()
                reporter.log("llm_start", "Batch relation LLM call started", edges=len(edges))
                try:
                    batch_relations = generate_relations_batch(
                        [edge.__dict__ for edge in edges],
                        llm_settings=self.config.relation_llm_settings,
                        attempt_hook=emit_attempt_progress,
                    )
                except Exception as exc:
                    reporter.advance_llm()
                    reporter.log(
                        "llm_error",
                        "Batch relation generation failed; fallback applied",
                        level="warn",
                        edges=len(edges),
                        elapsed_ms=int((time.perf_counter() - batch_t0) * 1000),
                        error=str(exc),
                    )
                    for edge in edges:
                        edge.relation_label = "other"
                        edge.relation_explanation = f"fallback_on_error: {exc}"
                else:
                    reporter.advance_llm()
                    for edge in edges:
                        relation = batch_relations.get(edge.edge_id, {})
                        edge.relation_label = relation.get("relation_label") or "other"
                        edge.relation_explanation = relation.get("relation_explanation") or "llm batch relation generation"
                    reporter.log(
                        "llm_done",
                        "Batch relation LLM call finished",
                        level="ok",
                        edges=len(edges),
                        elapsed_ms=int((time.perf_counter() - batch_t0) * 1000),
                    )

            metadata = GraphMetadata(
                graph_id=make_graph_id(root_entity.wikipedia_title),
                seed_entity=root_entity.wikipedia_title,
                root_url=root_entity.url,
                language=self.config.language,
                max_nodes=self.config.max_nodes,
                max_edges=resolved_max_edges,
                max_depth=int(self.config.max_depth or 0),
                max_neighbors_per_node=self.config.max_neighbors_per_node,
                root_max_neighbors_per_node=self._neighbor_limit_for_depth(0),
                build_time_utc=utc_now_iso(),
                relation_mode=self.config.relation_mode,
                root_expand_all_intro_links=bool(self.config.root_expand_all_intro_links),
                root_link_selection_mode=str(self.config.root_link_selection_mode or "intro"),
                root_full_page_prompt_file=str(self.config.root_full_page_prompt_file or "graph_root_full_page_selection.md"),
                root_full_page_llm_target_neighbors=int(self.config.root_full_page_llm_target_neighbors),
                neighbor_selection_mode=str(self.config.neighbor_selection_mode or "heuristic"),
                llm_candidate_pool_limit=int(self.config.llm_candidate_pool_limit),
                root_llm_candidate_pool_limit=self._root_llm_candidate_pool_limit(),
                page_fetch_concurrency=max(1, int(self.config.page_fetch_concurrency)),
                page_fetch_attempt_stats=self._page_fetch_attempt_stats(),
            )
            total_ms = int((time.perf_counter() - build_t0) * 1000)
            reporter.log(
                "build_done",
                "Graph build completed",
                level="ok",
                elapsed_ms=total_ms,
                entities=len(entities_by_title),
                edges=len(edges),
            )
            self._emit_progress(
                "build_done",
                root_title=root_entity.wikipedia_title,
                depth=max((entity.depth for entity in entities_by_title.values()), default=0),
                nodes=len(entities_by_title),
            )
            return GraphData(
                metadata=metadata,
                entities=list(entities_by_title.values()),
                edges=edges,
                diagnostics={
                    "page_fetch_attempt_records": self._page_fetch_attempt_snapshot(),
                    "node_expansion_records": node_expansion_records,
                },
            )
        finally:
            self._active_reporter = None
            reporter.stop()

    def _neighbor_limit_for_depth(self, depth: int) -> int:
        """Return the expansion width limit for a node at the given BFS depth."""
        if depth == 0 and self._use_root_full_page_llm_selection():
            return max(0, int(self.config.root_full_page_llm_target_neighbors))
        if depth == 0 and self.config.root_max_neighbors_per_node is not None:
            return max(0, int(self.config.root_max_neighbors_per_node))
        return max(0, int(self.config.max_neighbors_per_node))

    def _use_root_full_page_llm_selection(self) -> bool:
        """Whether the root should use full-page LLM-assisted first-hop selection."""
        return str(self.config.root_link_selection_mode or "intro").strip().lower() == "full_page_llm"

    def _should_use_root_full_page_llm_selection(self, depth: int) -> bool:
        return bool(depth == 0 and self._use_root_full_page_llm_selection())

    def _use_intro_only_expansion(self) -> bool:
        """Whether graph expansion should use intro-only anchors without supplemental scans."""
        return bool(self.config.root_expand_all_intro_links or self.config.neighbor_selection_mode == "llm")

    def _use_full_page_neighbor_candidates(self, depth: int) -> bool:
        """Return True when a non-root node should expose full-page anchors to LLM selection."""
        if self.config.neighbor_selection_mode != "llm":
            return False
        if int(depth) <= 0:
            return False
        if self.config.max_depth is not None and int(depth) >= int(self.config.max_depth):
            return False
        return True

    def _should_expand_all_root_links(self, depth: int) -> bool:
        """Return True when the root should expand to all intro anchors."""
        return bool(
            depth == 0
            and self.config.root_expand_all_intro_links
            and not self._use_root_full_page_llm_selection()
            and self.config.root_max_neighbors_per_node is None
        )

    def _root_llm_candidate_pool_limit(self) -> int:
        """Return root-specific llm candidate pool size, falling back to the generic pool."""
        if self.config.root_llm_candidate_pool_limit is None:
            return max(0, int(self.config.llm_candidate_pool_limit))
        return max(0, int(self.config.root_llm_candidate_pool_limit))

    def _llm_candidate_pool_limit_for_depth(self, depth: int) -> int:
        """Return prompt-side candidate pool limit for the given source depth."""
        if self._should_use_root_full_page_llm_selection(int(depth)):
            return self._root_llm_candidate_pool_limit()
        return max(0, int(self.config.llm_candidate_pool_limit))

    def _truncate_candidates_for_llm(self, anchors: list[AnchorEvidence], depth: int) -> list[AnchorEvidence]:
        """Bound the prompt-side llm candidate pool while preserving original order."""
        pool_limit = self._llm_candidate_pool_limit_for_depth(depth)
        if pool_limit <= 0 or len(anchors) <= pool_limit:
            return list(anchors)
        return list(anchors[:pool_limit])

    def _should_use_llm_neighbor_selection(
        self,
        depth: int,
        llm_candidates: list[AnchorEvidence],
        limit: int,
    ) -> bool:
        """Return True when the current node should use llm neighbor selection."""
        return (
            self.config.neighbor_selection_mode == "llm"
            and depth > 0
            and limit > 0
            and bool(llm_candidates)
        )

    def _lead_intro_anchors(
        self,
        source_entity: EntityNode,
        candidates: list[AnchorEvidence],
    ) -> list[AnchorEvidence]:
        """Return root lead-section candidates for full-page prompt context."""
        intro_paragraph_count = max(0, len(source_entity.text or []))
        if intro_paragraph_count <= 0:
            return []
        lead: list[AnchorEvidence] = []
        seen_titles: set[str] = set()
        for anchor in candidates:
            title = str(anchor.wikipedia_title).strip()
            if not title or title in seen_titles:
                continue
            if int(anchor.paragraph_id) < intro_paragraph_count:
                lead.append(anchor)
                seen_titles.add(title)
        return lead

    def _select_root_full_page_fallback(
        self,
        source_entity: EntityNode,
        candidates: list[AnchorEvidence],
        limit: int,
    ) -> list[AnchorEvidence]:
        """Fallback for full-page root selection: diversify across all lead/body links."""
        return self._fill_selected_neighbors(
            candidates,
            [],
            max(0, int(limit)),
            avoid_similar_titles=True,
        )

    def _fallback_root_relation_explanation(
        self,
        *,
        root_title: str,
        target_title: str,
        anchor: AnchorEvidence,
    ) -> str:
        """Build a conservative root first-hop relation explanation from anchor evidence."""
        anchor_text = str(anchor.text or "").strip() or str(target_title).strip()
        context = safe_snippet(str(anchor.context_text or "").strip(), max_chars=260)
        if context:
            return (
                f"The root article '{root_title}' links to '{target_title}' via anchor text "
                f"'{anchor_text}' in this context: {context}"
            )
        return f"The root article '{root_title}' directly links to '{target_title}' as a selected first-hop expansion entity."

    def _neighbor_selection_concurrency(self) -> int:
        """Resolve llm concurrency for graph neighbor selection."""
        settings = self.config.neighbor_selection_llm_settings or {}
        try:
            return max(1, int(settings.get("concurrency", 1)))
        except Exception:
            return 1

    def _select_neighbors_for_frontier(
        self,
        level_plans: list[dict[str, Any]],
        reporter: GraphBuildReporter,
        emit_attempt_progress: Callable[[dict[str, object]], None],
    ) -> dict[str, list[AnchorEvidence]]:
        """Run neighbor selection for one BFS frontier with concurrent llm calls."""
        results: dict[str, list[AnchorEvidence]] = {}
        llm_plans = [plan for plan in level_plans if bool(plan.get("use_llm"))]

        for plan in level_plans:
            source_title = str(plan["source_title"])
            source_entity = plan["source_entity"]
            source_depth = int(plan["source_depth"])
            if self._should_expand_all_root_links(source_depth):
                results[source_title] = list(plan["candidates"])
                continue
            if not bool(plan.get("use_llm")):
                plan["selection_fallback"] = False
                plan["selection_error"] = ""
                results[source_title] = self._select_diverse_neighbors(
                    list(plan["candidates"]),
                    int(plan["neighbor_limit"]),
                )

        if not llm_plans:
            return results

        max_workers = min(len(llm_plans), self._neighbor_selection_concurrency())
        if max_workers <= 1:
            for plan in llm_plans:
                source_title = str(plan["source_title"])
                selected, elapsed_ms, error = self._run_llm_neighbor_selection(plan, emit_attempt_progress)
                reporter.advance_llm()
                if error:
                    plan["selection_fallback"] = True
                    plan["selection_error"] = error
                    reporter.log(
                        "llm_error",
                        "Neighbor-selection LLM failed; fallback heuristic applied",
                        level="warn",
                        source=source_title,
                        depth=int(plan["source_depth"]),
                        elapsed_ms=elapsed_ms,
                        error=error,
                    )
                    if self._should_use_root_full_page_llm_selection(int(plan["source_depth"])):
                        results[source_title] = self._select_root_full_page_fallback(
                            source_entity=plan["source_entity"],
                            candidates=list(plan["candidates"]),
                            limit=int(plan["neighbor_limit"]),
                        )
                    else:
                        results[source_title] = self._select_diverse_neighbors(
                            list(plan["candidates"]),
                            int(plan["neighbor_limit"]),
                        )
                    continue
                plan["selection_fallback"] = False
                plan["selection_error"] = ""
                reporter.log(
                    "llm_done",
                    "Neighbor-selection LLM call finished",
                    level="ok",
                    source=source_title,
                    depth=int(plan["source_depth"]),
                    elapsed_ms=elapsed_ms,
                    selected=len(selected),
                )
                results[source_title] = self._fill_selected_neighbors(
                    candidates=list(plan["candidates"]),
                    selected=list(selected),
                    limit=int(plan["neighbor_limit"]),
                    avoid_similar_titles=self._should_use_root_full_page_llm_selection(int(plan["source_depth"])),
                )
            return results

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for plan in llm_plans:
                source_title = str(plan["source_title"])
                reporter.log(
                    "llm_start",
                    "Neighbor-selection LLM call started",
                    source=source_title,
                    depth=int(plan["source_depth"]),
                    candidate_count=len(plan["candidates"]),
                    llm_candidate_count=len(plan["llm_candidates"]),
                    target_count=int(plan["neighbor_limit"]),
                )
                future = executor.submit(self._run_llm_neighbor_selection, plan, emit_attempt_progress)
                futures[future] = plan
            for future in as_completed(futures):
                plan = futures[future]
                source_title = str(plan["source_title"])
                selected, elapsed_ms, error = future.result()
                reporter.advance_llm()
                if error:
                    plan["selection_fallback"] = True
                    plan["selection_error"] = error
                    reporter.log(
                        "llm_error",
                        "Neighbor-selection LLM failed; fallback heuristic applied",
                        level="warn",
                        source=source_title,
                        depth=int(plan["source_depth"]),
                        elapsed_ms=elapsed_ms,
                        error=error,
                    )
                    if self._should_use_root_full_page_llm_selection(int(plan["source_depth"])):
                        results[source_title] = self._select_root_full_page_fallback(
                            source_entity=plan["source_entity"],
                            candidates=list(plan["candidates"]),
                            limit=int(plan["neighbor_limit"]),
                        )
                    else:
                        results[source_title] = self._select_diverse_neighbors(
                            list(plan["candidates"]),
                            int(plan["neighbor_limit"]),
                        )
                    continue
                plan["selection_fallback"] = False
                plan["selection_error"] = ""
                reporter.log(
                    "llm_done",
                    "Neighbor-selection LLM call finished",
                    level="ok",
                    source=source_title,
                    depth=int(plan["source_depth"]),
                    elapsed_ms=elapsed_ms,
                    selected=len(selected),
                )
                results[source_title] = self._fill_selected_neighbors(
                    candidates=list(plan["candidates"]),
                    selected=list(selected),
                    limit=int(plan["neighbor_limit"]),
                    avoid_similar_titles=self._should_use_root_full_page_llm_selection(int(plan["source_depth"])),
                )
        return results

    def _run_llm_neighbor_selection(
        self,
        plan: dict[str, Any],
        emit_attempt_progress: Callable[[dict[str, object]], None],
    ) -> tuple[list[AnchorEvidence], int, str]:
        """Execute one llm neighbor-selection call and capture runtime/error."""
        select_t0 = time.perf_counter()
        try:
            if self._should_use_root_full_page_llm_selection(int(plan["source_depth"])):
                selected = select_root_full_page_neighbors_llm(
                    source_entity=plan["source_entity"],
                    candidate_anchors=list(plan["llm_candidates"]),
                    lead_intro_anchors=self._lead_intro_anchors(plan["source_entity"], list(plan["candidates"])),
                    limit=min(int(plan["neighbor_limit"]), len(plan["llm_candidates"])),
                    llm_settings=self.config.neighbor_selection_llm_settings or {},
                    attempt_hook=emit_attempt_progress,
                    prompt_file=str(self.config.root_full_page_prompt_file or "graph_root_full_page_selection.md"),
                )
            else:
                selected = select_neighbors_llm(
                    source_entity=plan["source_entity"],
                    candidate_anchors=list(plan["llm_candidates"]),
                    limit=min(int(plan["neighbor_limit"]), len(plan["llm_candidates"])),
                    llm_settings=self.config.neighbor_selection_llm_settings or {},
                    attempt_hook=emit_attempt_progress,
                )
        except Exception as exc:
            return [], int((time.perf_counter() - select_t0) * 1000), str(exc)
        return list(selected), int((time.perf_counter() - select_t0) * 1000), ""

    def _fill_selected_neighbors(
        self,
        candidates: list[AnchorEvidence],
        selected: list[AnchorEvidence],
        limit: int,
        *,
        avoid_similar_titles: bool = False,
    ) -> list[AnchorEvidence]:
        """Backfill llm selections with heuristic candidates when fewer than limit were returned."""
        if limit <= 0:
            return []
        deduped: list[AnchorEvidence] = []
        seen_titles: set[str] = set()
        seen_token_sets: list[set[str]] = []
        for anchor in selected:
            title = str(anchor.wikipedia_title).strip()
            if not title or title in seen_titles:
                continue
            token_set = _title_similarity_tokens(title)
            if avoid_similar_titles and _is_near_duplicate_title(token_set, seen_token_sets):
                continue
            deduped.append(anchor)
            seen_titles.add(title)
            seen_token_sets.append(token_set)
            if len(deduped) >= limit:
                return deduped
        for anchor in self._select_diverse_neighbors(candidates, len(candidates)):
            if len(deduped) >= limit:
                break
            title = str(anchor.wikipedia_title).strip()
            if not title or title in seen_titles:
                continue
            token_set = _title_similarity_tokens(title)
            if avoid_similar_titles and _is_near_duplicate_title(token_set, seen_token_sets):
                continue
            deduped.append(anchor)
            seen_titles.add(title)
            seen_token_sets.append(token_set)
        return deduped[:limit]

    def _accept_selected_neighbors(
        self,
        selected: list[AnchorEvidence],
        candidates: list[AnchorEvidence],
        reserved_titles: set[str],
        limit: int,
        expand_all: bool,
    ) -> list[AnchorEvidence]:
        """Accept selected anchors in parent order while preventing cross-sibling duplicate targets."""
        accepted: list[AnchorEvidence] = []
        accepted_titles: set[str] = set()
        for anchor in selected:
            title = str(anchor.wikipedia_title).strip()
            if not title or title in reserved_titles or title in accepted_titles:
                continue
            accepted.append(anchor)
            accepted_titles.add(title)
            reserved_titles.add(title)
            if not expand_all and len(accepted) >= limit:
                return accepted
        if expand_all:
            return accepted
        for anchor in candidates:
            title = str(anchor.wikipedia_title).strip()
            if not title or title in reserved_titles or title in accepted_titles:
                continue
            accepted.append(anchor)
            accepted_titles.add(title)
            reserved_titles.add(title)
            if len(accepted) >= limit:
                break
        return accepted

    def _resolve_targets_for_frontier(
        self,
        resolve_tasks: list[tuple[str, AnchorEvidence, EntityNode]],
    ) -> dict[tuple[str, str], tuple[EntityNode, bool, int]]:
        """Resolve selected targets for a whole frontier with shared page-fetch concurrency."""
        if not resolve_tasks:
            return {}

        max_workers = min(len(resolve_tasks), max(1, int(self.config.page_fetch_concurrency)))
        if max_workers <= 1:
            results: dict[tuple[str, str], tuple[EntityNode, bool, int]] = {}
            for source_title, anchor, source_entity in resolve_tasks:
                resolve_t0 = time.perf_counter()
                target_entity, can_expand = self._resolve_target_entity(
                    anchor,
                    depth=source_entity.depth + 1,
                    source_id=source_entity._id,
                    source_title=source_entity.wikipedia_title,
                )
                results[(source_title, anchor.anchor_id)] = (
                    target_entity,
                    can_expand,
                    int((time.perf_counter() - resolve_t0) * 1000),
                )
            return results

        results: dict[tuple[str, str], tuple[EntityNode, bool, int]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._resolve_target_entity,
                    anchor,
                    source_entity.depth + 1,
                    source_entity._id,
                    source_entity.wikipedia_title,
                ): (source_title, anchor)
                for source_title, anchor, source_entity in resolve_tasks
            }
            started_at = {future: time.perf_counter() for future in futures}
            for future in as_completed(futures):
                source_title, anchor = futures[future]
                target_entity, can_expand = future.result()
                results[(source_title, anchor.anchor_id)] = (
                    target_entity,
                    can_expand,
                    int((time.perf_counter() - started_at[future]) * 1000),
                )
        return results

    def _select_neighbors(
        self,
        source_entity: EntityNode,
        candidates: list[AnchorEvidence],
        llm_candidates: list[AnchorEvidence],
        limit: int,
        reporter: GraphBuildReporter,
        emit_attempt_progress: Callable[[dict[str, object]], None],
    ) -> list[AnchorEvidence]:
        """Select outgoing anchors for the current source node."""
        if self._should_expand_all_root_links(source_entity.depth):
            return list(candidates)
        if limit <= 0 or not candidates:
            return []
        if self._should_use_root_full_page_llm_selection(source_entity.depth):
            effective_limit = int(limit)
            root_llm_candidates = self._truncate_candidates_for_llm(candidates, depth=source_entity.depth)
            reporter.log(
                "llm_start",
                "Root full-page neighbor-selection LLM call started",
                source=source_entity.wikipedia_title,
                depth=source_entity.depth,
                candidate_count=len(root_llm_candidates),
                original_candidate_count=len(candidates),
                target_count=effective_limit,
            )
            select_t0 = time.perf_counter()
            try:
                selected = select_root_full_page_neighbors_llm(
                    source_entity=source_entity,
                    candidate_anchors=root_llm_candidates,
                    lead_intro_anchors=self._lead_intro_anchors(source_entity, candidates),
                    limit=min(effective_limit, len(root_llm_candidates)),
                    llm_settings=self.config.neighbor_selection_llm_settings or {},
                    attempt_hook=emit_attempt_progress,
                )
            except Exception as exc:
                reporter.advance_llm()
                reporter.log(
                    "llm_error",
                    "Root full-page neighbor-selection LLM failed; fallback heuristic applied",
                    level="warn",
                    source=source_entity.wikipedia_title,
                    depth=source_entity.depth,
                    elapsed_ms=int((time.perf_counter() - select_t0) * 1000),
                    error=str(exc),
                )
                return self._select_root_full_page_fallback(source_entity, candidates, effective_limit)
            reporter.advance_llm()
            reporter.log(
                "llm_done",
                "Root full-page neighbor-selection LLM call finished",
                level="ok",
                source=source_entity.wikipedia_title,
                depth=source_entity.depth,
                elapsed_ms=int((time.perf_counter() - select_t0) * 1000),
                selected=len(selected),
            )
            return self._fill_selected_neighbors(
                candidates,
                selected,
                effective_limit,
                avoid_similar_titles=True,
            )
        if self._should_use_llm_neighbor_selection(source_entity.depth, llm_candidates, limit):
            reporter.log(
                "llm_start",
                "Neighbor-selection LLM call started",
                source=source_entity.wikipedia_title,
                depth=source_entity.depth,
                candidate_count=len(candidates),
                llm_candidate_count=len(llm_candidates),
                target_count=limit,
            )
            select_t0 = time.perf_counter()
            try:
                selected = select_neighbors_llm(
                    source_entity=source_entity,
                    candidate_anchors=llm_candidates,
                    limit=min(limit, len(llm_candidates)),
                    llm_settings=self.config.neighbor_selection_llm_settings or {},
                    attempt_hook=emit_attempt_progress,
                )
            except Exception as exc:
                reporter.advance_llm()
                reporter.log(
                    "llm_error",
                    "Neighbor-selection LLM failed; fallback heuristic applied",
                    level="warn",
                    source=source_entity.wikipedia_title,
                    depth=source_entity.depth,
                    elapsed_ms=int((time.perf_counter() - select_t0) * 1000),
                    error=str(exc),
                )
                return self._select_diverse_neighbors(candidates, limit)
            reporter.advance_llm()
            if len(selected) < min(limit, len(candidates)):
                selected_titles = {str(anchor.wikipedia_title).strip() for anchor in selected if str(anchor.wikipedia_title).strip()}
                remainder = [anchor for anchor in candidates if str(anchor.wikipedia_title).strip() not in selected_titles]
                selected.extend(self._select_diverse_neighbors(remainder, limit - len(selected)))
            reporter.log(
                "llm_done",
                "Neighbor-selection LLM call finished",
                level="ok",
                source=source_entity.wikipedia_title,
                depth=source_entity.depth,
                elapsed_ms=int((time.perf_counter() - select_t0) * 1000),
                selected=len(selected),
            )
            return selected
        return self._select_diverse_neighbors(candidates, limit)

    def _resolve_selected_targets(
        self,
        selected: list[AnchorEvidence],
        source_entity: EntityNode,
    ) -> list[tuple[AnchorEvidence, EntityNode, bool, int]]:
        """Resolve selected anchors into target entities, keeping selected order stable."""
        if not selected:
            return []

        max_workers = max(1, int(self.config.page_fetch_concurrency))
        if max_workers <= 1 or len(selected) <= 1:
            results: list[tuple[AnchorEvidence, EntityNode, bool, int]] = []
            for anchor in selected:
                resolve_t0 = time.perf_counter()
                target_entity, can_expand = self._resolve_target_entity(
                    anchor,
                    depth=source_entity.depth + 1,
                    source_id=source_entity._id,
                    source_title=source_entity.wikipedia_title,
                )
                results.append((anchor, target_entity, can_expand, int((time.perf_counter() - resolve_t0) * 1000)))
            return results

        results_by_index: dict[int, tuple[AnchorEvidence, EntityNode, bool, int]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._resolve_target_entity,
                    anchor,
                    source_entity.depth + 1,
                    source_entity._id,
                    source_entity.wikipedia_title,
                ): (idx, anchor)
                for idx, anchor in enumerate(selected)
            }
            future_started_at: dict[object, float] = {future: time.perf_counter() for future in futures}
            for future in as_completed(futures):
                idx, anchor = futures[future]
                target_entity, can_expand = future.result()
                resolve_ms = int((time.perf_counter() - future_started_at[future]) * 1000)
                results_by_index[idx] = (anchor, target_entity, can_expand, resolve_ms)
        return [results_by_index[idx] for idx in range(len(selected)) if idx in results_by_index]

    def _fetch_and_parse_page(
        self,
        title: str,
        *,
        supplement_anchor_target_count: int | None = None,
        include_supplemental_tables: bool = False,
        collect_all_supplemental_anchors: bool = False,
        include_supplemental_lists: bool = False,
    ) -> ParsedEntityPage:
        """Fetch and parse a Wikipedia page into intro text and anchors."""
        fetch_id = uuid.uuid4().hex[:16]
        fetch_t0 = time.perf_counter()
        reporter = self._active_reporter
        if reporter is not None:
            reporter.log(
                "page_fetch_start",
                "Wikipedia page fetch started",
                level="info",
                fetch_id=fetch_id,
                requested_title=str(title).strip(),
            )
        try:
            page = self.client.fetch_page(title)
        except WikipediaRequestError as exc:
            self._record_page_fetch_attempt(
                fetch_id=fetch_id,
                requested_title=title,
                final_title="",
                attempt_count=int(getattr(exc, "attempt_count", 0) or 0),
                success=False,
                error=str(exc),
                elapsed_ms=int((time.perf_counter() - fetch_t0) * 1000),
            )
            raise
        self._record_page_fetch_attempt(
            fetch_id=fetch_id,
            requested_title=page.requested_title,
            final_title=page.final_title,
            attempt_count=page.attempt_count,
            success=True,
            error="",
            elapsed_ms=int((time.perf_counter() - fetch_t0) * 1000),
        )
        parsed = self.parser.parse_page(
            html=page.html,
            source_title=page.final_title,
            source_url=page.url,
            supplement_anchor_target_count=supplement_anchor_target_count,
            include_supplemental_tables=include_supplemental_tables,
            collect_all_supplemental_anchors=collect_all_supplemental_anchors,
            include_supplemental_lists=include_supplemental_lists,
        )
        if self.config.sleep_seconds > 0:
            time.sleep(self.config.sleep_seconds)
        return parsed

    def _reset_page_fetch_attempt_records(self) -> None:
        with self._page_fetch_lock:
            self._page_fetch_attempt_records = []

    def _record_page_fetch_attempt(
        self,
        *,
        fetch_id: str,
        requested_title: str,
        final_title: str,
        attempt_count: int,
        success: bool,
        error: str,
        elapsed_ms: int,
    ) -> None:
        record = {
            "fetch_id": str(fetch_id).strip(),
            "requested_title": str(requested_title).strip(),
            "final_title": str(final_title).strip(),
            "attempt_count": max(0, int(attempt_count)),
            "success": bool(success),
            "error": str(error).strip(),
            "elapsed_ms": max(0, int(elapsed_ms)),
        }
        with self._page_fetch_lock:
            self._page_fetch_attempt_records.append(record)
        reporter = self._active_reporter
        if reporter is not None:
            reporter.log(
                "page_fetch",
                "Wikipedia page fetch succeeded" if success else "Wikipedia page fetch failed; stub node may be used",
                level="ok" if success else "warn",
                **record,
            )

    def _page_fetch_attempt_snapshot(self) -> list[dict[str, Any]]:
        with self._page_fetch_lock:
            return [dict(record) for record in self._page_fetch_attempt_records]

    def _page_fetch_attempt_stats(self) -> dict[str, Any]:
        records = self._page_fetch_attempt_snapshot()
        request_count = len(records)
        success_records = [record for record in records if record.get("success")]
        error_records = [record for record in records if not record.get("success")]
        attempt_counts = [int(record.get("attempt_count", 0) or 0) for record in records]
        success_attempt_counts = [int(record.get("attempt_count", 0) or 0) for record in success_records]
        error_attempt_counts = [int(record.get("attempt_count", 0) or 0) for record in error_records]

        def average(values: list[int]) -> float:
            return round(sum(values) / len(values), 4) if values else 0.0

        return {
            "request_count": request_count,
            "success_count": len(success_records),
            "error_count": len(error_records),
            "attempt_count_total": sum(attempt_counts),
            "attempt_count_avg": average(attempt_counts),
            "success_attempt_count_avg": average(success_attempt_counts),
            "error_attempt_count_avg": average(error_attempt_counts),
            "max_attempt_count": max(attempt_counts, default=0),
            "retried_request_count": sum(1 for value in attempt_counts if value > 1),
        }

    def _to_entity_node(
        self,
        parsed: ParsedEntityPage,
        node_id: str,
        depth: int,
        source_id: str,
        source_title: str,
    ) -> EntityNode:
        """Convert parsed page into graph entity schema."""
        anchors = [self._to_anchor_evidence(anchor) for anchor in parsed.anchors]
        wikipedia_id = parsed.wikipedia_title.replace(" ", "_")
        snippet = safe_snippet(parsed.text[0]) if parsed.text else ""
        return EntityNode(
            _id=node_id,
            wikipedia_id=wikipedia_id,
            wikipedia_title=parsed.wikipedia_title,
            url=parsed.url,
            depth=depth,
            source_id=source_id,
            source_title=source_title,
            text=parsed.text,
            text_snippet=snippet,
            anchors=anchors,
        )

    def _to_anchor_evidence(self, parsed_anchor: ParsedAnchor) -> AnchorEvidence:
        """Map parser anchor into graph schema anchor."""
        return AnchorEvidence(
            anchor_id=parsed_anchor.anchor_id,
            wikipedia_title=parsed_anchor.wikipedia_title,
            href=parsed_anchor.href,
            text=parsed_anchor.text,
            paragraph_id=parsed_anchor.paragraph_id,
            start=parsed_anchor.start,
            end=parsed_anchor.end,
            context_text=parsed_anchor.context_text,
            source_title=parsed_anchor.source_title,
            source_url=parsed_anchor.source_url,
            target_url=parsed_anchor.target_url,
            wikipedia_id=parsed_anchor.wikipedia_id,
        )

    def _candidate_anchors(
        self,
        source_entity: EntityNode,
        entities_by_title: dict[str, EntityNode],
    ) -> list[AnchorEvidence]:
        """Collect unique candidate anchors that can introduce new nodes."""
        best_by_target: dict[str, AnchorEvidence] = {}
        for anchor in source_entity.anchors:
            if not anchor.wikipedia_title:
                continue
            if anchor.wikipedia_title == source_entity.wikipedia_title:
                continue
            if anchor.wikipedia_title in entities_by_title:
                continue
            current = best_by_target.get(anchor.wikipedia_title)
            if current is None or self._anchor_order_key(anchor) < self._anchor_order_key(current):
                best_by_target[anchor.wikipedia_title] = anchor
        return sorted(best_by_target.values(), key=self._anchor_order_key)

    def _anchor_order_key(self, anchor: AnchorEvidence) -> tuple[int, int, int, str]:
        """Stable ordering for choosing a representative anchor per target."""
        return (
            int(anchor.paragraph_id),
            int(anchor.start),
            int(anchor.end),
            str(anchor.wikipedia_title),
        )

    def _select_diverse_neighbors(self, anchors: list[AnchorEvidence], limit: int) -> list[AnchorEvidence]:
        """
        Select neighbors with simple diversity priorities.

        Inspired by the multi-pass neighbor selection strategy in ref/xingyu/wikidata_new.py:
        1) paragraph diversity第一轮优先“不同段落”的 anchor
        2) target-title diversity第二轮补“不同 target”的 anchor
        3) fill by original order第三轮按原顺序补满
        所以最终数量是 min(候选数, max_neighbors_per_node)。
        """
        if limit <= 0 or not anchors:
            return []

        selected: list[AnchorEvidence] = []
        used_paragraphs: set[int] = set()
        used_targets: set[str] = set()

        for anchor in anchors:
            if len(selected) >= limit:
                return selected
            if anchor.paragraph_id in used_paragraphs:
                continue
            if anchor.wikipedia_title in used_targets:
                continue
            selected.append(anchor)
            used_paragraphs.add(anchor.paragraph_id)
            used_targets.add(anchor.wikipedia_title)

        for anchor in anchors:
            if len(selected) >= limit:
                return selected
            if anchor in selected:
                continue
            if anchor.wikipedia_title in used_targets:
                continue
            selected.append(anchor)
            used_targets.add(anchor.wikipedia_title)

        for anchor in anchors:
            if len(selected) >= limit:
                break
            if anchor in selected:
                continue
            if anchor.wikipedia_title in used_targets:
                continue
            selected.append(anchor)
            used_targets.add(anchor.wikipedia_title)
        return selected

    def _resolve_target_entity(
        self,
        anchor: AnchorEvidence,
        depth: int,
        source_id: str,
        source_title: str,
    ) -> tuple[EntityNode, bool]:
        """
        Resolve anchor target page to entity node.

        Returns:
            (entity, can_expand)
            can_expand=False means fetch failed and we created a stub node.
        """
        use_full_page_neighbor_candidates = self._use_full_page_neighbor_candidates(depth)
        try:
            parsed = self._fetch_and_parse_page(
                anchor.wikipedia_title,
                supplement_anchor_target_count=(
                    None
                    if (use_full_page_neighbor_candidates or self._use_intro_only_expansion())
                    else self._neighbor_limit_for_depth(depth)
                ),
                include_supplemental_tables=False if use_full_page_neighbor_candidates else not self._use_intro_only_expansion(),
                collect_all_supplemental_anchors=use_full_page_neighbor_candidates,
                include_supplemental_lists=use_full_page_neighbor_candidates,
            )
            return self._to_entity_node(
                parsed,
                node_id="",
                depth=depth,
                source_id=source_id,
                source_title=source_title,
            ), True
        except WikipediaRequestError:
            stub_title = anchor.wikipedia_title
            stub_entity = EntityNode(
                _id="",
                wikipedia_id=stub_title.replace(" ", "_"),
                wikipedia_title=stub_title,
                url=anchor.target_url,
                depth=depth,
                source_id=source_id,
                source_title=source_title,
                text=[],
                text_snippet="",
                anchors=[],
            )
            return stub_entity, False
