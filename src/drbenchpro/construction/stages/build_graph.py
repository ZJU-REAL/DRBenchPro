"""Pipeline entry for building a local Wikipedia graph."""

from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from drbenchpro.construction.graph.graph_builder import GraphBuildConfig, GraphBuilder
from drbenchpro.construction.graph.graph_structure import attach_realized_out_neighbors, build_compact_raw_graph_payload
from drbenchpro.construction.graph.relation_generator import generate_relation, generate_relations_batch
from drbenchpro.construction.graph.wikipedia_client import WikipediaClient
from drbenchpro.construction.graph.wikipedia_parser import WikipediaParser
from drbenchpro.llm.client import (
    DEFAULT_LLM_BACKOFF_SECONDS,
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_LLM_TIMEOUT,
)


def build_graph_pipeline(
    config: dict[str, Any],
    root_title: str | None = None,
    random_root: bool = False,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build graph artifact using merged config and optional root override."""
    crawler = config.get("crawler", {})
    graph_cfg = config.get("graph", {})
    relation = config.get("relation", {})
    language = crawler.get("language", "en")
    timeout = int(crawler.get("timeout", 15))
    max_retries = int(crawler.get("max_retries", 0))
    user_agent = str(crawler.get("user_agent", "DRBenchProBot/0.1"))

    client = WikipediaClient(
        language=language,
        timeout=timeout,
        max_retries=max_retries,
        user_agent=user_agent,
    )
    parser = WikipediaParser(language=language)

    relation_mode = str(relation.get("mode", "none"))
    relation_llm_settings = _resolve_relation_llm_settings(config, relation_mode=relation_mode)
    neighbor_selection_mode = str(graph_cfg.get("neighbor_selection_mode", "heuristic") or "heuristic").lower()
    root_link_selection_mode = str(graph_cfg.get("root_link_selection_mode", "intro") or "intro").lower()
    neighbor_selection_llm_settings = _resolve_neighbor_selection_llm_settings(
        config,
        neighbor_selection_mode=neighbor_selection_mode,
        root_link_selection_mode=root_link_selection_mode,
    )

    build_cfg = GraphBuildConfig(
        language=language,
        max_nodes=int(graph_cfg.get("max_nodes", 20)),
        max_edges=int(graph_cfg["max_edges"]) if graph_cfg.get("max_edges") is not None else None,
        max_depth=int(graph_cfg["max_depth"]) if graph_cfg.get("max_depth") is not None else None,
        max_neighbors_per_node=int(graph_cfg.get("max_neighbors_per_node", 3)),
        root_max_neighbors_per_node=(
            int(graph_cfg["root_max_neighbors_per_node"])
            if graph_cfg.get("root_max_neighbors_per_node") is not None
            else None
        ),
        root_expand_all_intro_links=bool(graph_cfg.get("root_expand_all_intro_links", False)),
        root_link_selection_mode=root_link_selection_mode,
        root_full_page_prompt_file=str(
            graph_cfg.get("root_full_page_prompt_file", "graph_root_full_page_selection.md") or "graph_root_full_page_selection.md"
        ),
        root_full_page_llm_target_neighbors=max(0, int(graph_cfg.get("root_full_page_llm_target_neighbors", 30) or 0)),
        neighbor_selection_mode=neighbor_selection_mode,
        llm_candidate_pool_limit=int(graph_cfg.get("llm_candidate_pool_limit", 64)),
        root_llm_candidate_pool_limit=(
            int(graph_cfg["root_llm_candidate_pool_limit"])
            if graph_cfg.get("root_llm_candidate_pool_limit") is not None
            else None
        ),
        page_fetch_concurrency=max(1, int(graph_cfg.get("page_fetch_concurrency", 8))),
        sleep_seconds=float(graph_cfg.get("sleep_seconds", 0.2)),
        show_progress=bool(graph_cfg.get("show_progress", False)),
        progress_log_file=str(graph_cfg.get("progress_log_file") or ""),
        progress_jsonl_file=str(graph_cfg.get("progress_jsonl_file") or ""),
        progress_command=str(graph_cfg.get("progress_command") or ""),
        progress_hook=progress_hook,
        relation_mode=relation_mode,
        relation_llm_settings=relation_llm_settings,
        neighbor_selection_llm_settings=neighbor_selection_llm_settings,
    )
    builder = GraphBuilder(client=client, parser=parser, config=build_cfg)
    graph = builder.build_graph(root_title=root_title, random_root=random_root)
    if str(graph_cfg.get("output_style", "")).strip() == "compact_raw":
        return build_compact_raw_graph_payload(graph)
    graph = attach_realized_out_neighbors(graph)
    return graph.to_dict()


def enrich_graph_relations_pipeline(
    graph: dict[str, Any],
    config: dict[str, Any],
    relation_mode: str | None = None,
    resume: bool = False,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    progress_hook: Callable[[str, str, dict[str, Any]], None] | None = None,
    partial_hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Attach relation annotations to an already-built raw graph artifact."""
    def emit(stage: str, message: str, **fields: Any) -> None:
        if progress_hook:
            progress_hook(stage, message, dict(fields))

    mode = str(relation_mode or config.get("relation", {}).get("mode", "none")).lower()
    if mode not in {"none", "rule", "llm", "llm_batch"}:
        raise ValueError(f"Unsupported relation mode: {mode}")

    enriched = copy.deepcopy(graph)
    enriched.setdefault("metadata", {})["relation_mode"] = mode
    edges = enriched.get("edges", [])
    llm_settings = _resolve_relation_llm_settings(config, relation_mode=mode)
    total_edges = len(edges)
    failed_edge_ids: set[str] = set(str(item) for item in enriched.get("metadata", {}).get("failed_relation_edge_ids", []) or [])

    if mode in {"llm", "llm_batch"} and total_edges:
        emit("relation_enrich_start", "Starting relation enrichment", mode=mode, edges=total_edges)

    if mode == "llm_batch" and edges:
        emit("relation_enrich_llm_start", "Calling LLM for batch relations", mode=mode, edges=total_edges, llm_calls=1)
        batch_relations = generate_relations_batch(
            edges,
            llm_settings=llm_settings,
            attempt_hook=attempt_hook,
        )
        for edge in edges:
            relation = batch_relations.get(str(edge.get("edge_id", "")).strip(), {})
            edge["relation_label"] = relation.get("relation_label") or "other"
            edge["relation_explanation"] = relation.get("relation_explanation") or "llm batch relation generation"
            edge["relation_mode"] = mode
        emit("relation_enrich_done", "Batch relation enrichment completed", mode=mode, edges=total_edges, completed=total_edges, llm_calls=1)
        return enriched

    if mode == "llm" and edges:
        concurrency = _resolve_relation_concurrency(llm_settings)
        if concurrency > 1:
            emit(
                "relation_enrich_llm_start",
                "Calling LLM for per-edge relations with concurrency",
                mode=mode,
                edges=total_edges,
                llm_calls=total_edges,
                concurrency=concurrency,
            )
            prepared_edges: list[tuple[int, dict[str, Any], dict[str, Any], str, str]] = []
            for idx, edge in enumerate(edges, start=1):
                source_title = str(edge.get("source_title", "")).strip()
                target_title = str(edge.get("target_title", "")).strip()
                payload = {
                    "source_title": source_title,
                    "target_title": target_title,
                    "anchor_text": edge.get("anchor_text", ""),
                    "context_text": edge.get("context_text", ""),
                    "href": edge.get("href", ""),
                    "target_text_snippet": edge.get("target_text_snippet", ""),
                }
                if resume and _relation_edge_completed(edge, mode):
                    emit(
                        "relation_enrich_item_skipped",
                        "Skipping already completed relation",
                        mode=mode,
                        current=idx,
                        total=total_edges,
                        edge_id=str(edge.get("edge_id", "")).strip(),
                        source_title=source_title,
                        target_title=target_title,
                    )
                    continue
                prepared_edges.append((idx, edge, payload, source_title, target_title))

            def emit_thread_attempt(event: dict[str, Any]) -> None:
                if attempt_hook is not None:
                    attempt_hook(event)

            results: dict[int, tuple[str, str, bool]] = {}
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(
                        _generate_relation_edge_safe,
                        payload,
                        llm_settings,
                        emit_thread_attempt,
                    ): (idx, edge, source_title, target_title)
                    for idx, edge, payload, source_title, target_title in prepared_edges
                }
                for future in as_completed(futures):
                    idx, edge, source_title, target_title = futures[future]
                    relation_label, relation_explanation, fallback = future.result()
                    results[idx] = (relation_label, relation_explanation, fallback)
                    edge["relation_label"] = relation_label
                    edge["relation_explanation"] = relation_explanation
                    edge["relation_mode"] = mode
                    edge_id = str(edge.get("edge_id", "")).strip()
                    if fallback:
                        failed_edge_ids.add(edge_id)
                    else:
                        failed_edge_ids.discard(edge_id)
                    enriched.setdefault("metadata", {})["failed_relation_edge_ids"] = sorted(failed_edge_ids)
                    emit(
                        "relation_enrich_item_done",
                        "Relation label generated" if not fallback else "Relation generation failed; fallback applied",
                        mode=mode,
                        current=idx,
                        total=total_edges,
                        edge_id=str(edge.get("edge_id", "")).strip(),
                        source_title=source_title,
                        target_title=target_title,
                        label=str(relation_label or ""),
                        llm_calls=1,
                        fallback=fallback,
                        concurrency=concurrency,
                    )
                    if partial_hook is not None:
                        partial_hook(enriched, {"edge_id": edge_id, "fallback": fallback, "mode": mode})

            emit(
                "relation_enrich_done",
                "Relation enrichment completed",
                mode=mode,
                edges=total_edges,
                completed=_count_completed_relation_edges(edges, mode),
                llm_calls=total_edges,
                concurrency=concurrency,
            )
            return enriched

    for idx, edge in enumerate(edges, start=1):
        source_title = str(edge.get("source_title", "")).strip()
        target_title = str(edge.get("target_title", "")).strip()
        payload = {
            "source_title": source_title,
            "target_title": target_title,
            "anchor_text": edge.get("anchor_text", ""),
            "context_text": edge.get("context_text", ""),
            "href": edge.get("href", ""),
            "target_text_snippet": edge.get("target_text_snippet", ""),
        }
        fallback = False
        if mode == "llm":
            if resume and _relation_edge_completed(edge, mode):
                emit(
                    "relation_enrich_item_skipped",
                    "Skipping already completed relation",
                    mode=mode,
                    current=idx,
                    total=total_edges,
                    edge_id=str(edge.get("edge_id", "")).strip(),
                    source_title=source_title,
                    target_title=target_title,
                )
                continue
            emit(
                "relation_enrich_item_start",
                "Generating relation label",
                mode=mode,
                current=idx,
                total=total_edges,
                edge_id=str(edge.get("edge_id", "")).strip(),
                source_title=source_title,
                target_title=target_title,
            )
        if mode == "llm":
            try:
                relation_label, relation_explanation = generate_relation(
                    payload,
                    mode=mode,
                    llm_settings=llm_settings,
                    attempt_hook=attempt_hook,
                )
            except Exception as exc:
                relation_label = "other"
                relation_explanation = f"fallback_on_error: {exc}"
                fallback = True
        else:
            relation_label, relation_explanation = generate_relation(
                payload,
                mode=mode,
                llm_settings=llm_settings,
                attempt_hook=attempt_hook,
            )

        edge["relation_label"] = relation_label
        edge["relation_explanation"] = relation_explanation
        edge["relation_mode"] = mode
        edge_id = str(edge.get("edge_id", "")).strip()
        if fallback:
            failed_edge_ids.add(edge_id)
        else:
            failed_edge_ids.discard(edge_id)
        enriched.setdefault("metadata", {})["failed_relation_edge_ids"] = sorted(failed_edge_ids)

        if mode == "llm":
            emit(
                "relation_enrich_item_done",
                "Relation label generated" if not fallback else "Relation generation failed; fallback applied",
                mode=mode,
                current=idx,
                total=total_edges,
                edge_id=str(edge.get("edge_id", "")).strip(),
                source_title=source_title,
                target_title=target_title,
                label=str(relation_label or ""),
                llm_calls=1,
                fallback=fallback,
            )
            if partial_hook is not None:
                partial_hook(enriched, {"edge_id": edge_id, "fallback": fallback, "mode": mode})

    if mode == "llm" and total_edges:
        emit(
            "relation_enrich_done",
            "Relation enrichment completed",
            mode=mode,
            edges=total_edges,
            completed=_count_completed_relation_edges(edges, mode),
            llm_calls=total_edges,
        )

    return enriched


def _resolve_relation_llm_settings(config: dict[str, Any], relation_mode: str) -> dict[str, Any] | None:
    """Resolve relation-generation llm settings when relation enrichment uses an llm."""
    if relation_mode not in {"llm", "llm_batch"}:
        return None

    llm = config.get("llm", {})
    relation_llm_settings = _build_llm_settings(
        llm,
        model_name=_resolve_llm_model_name(config, primary_key="relation"),
    )
    if not relation_llm_settings["api_key"]:
        raise ValueError("relation_mode=llm/llm_batch requires api key (env OPENAI_API_KEY or llm.api_key).")
    if not relation_llm_settings["base_url"]:
        raise ValueError("relation_mode=llm/llm_batch requires base url (env OPENAI_BASE_URL or llm.base_url).")
    if not relation_llm_settings["model"]:
        raise ValueError("relation_mode=llm/llm_batch requires llm.models.relation.")
    return relation_llm_settings


def _resolve_neighbor_selection_llm_settings(
    config: dict[str, Any],
    neighbor_selection_mode: str,
    root_link_selection_mode: str = "intro",
) -> dict[str, Any] | None:
    """Resolve llm settings when graph neighbor selection uses an llm."""
    if neighbor_selection_mode != "llm" and root_link_selection_mode != "full_page_llm":
        return None

    llm = config.get("llm", {})
    neighbor_llm_settings = _build_llm_settings(
        llm,
        model_name=_resolve_llm_model_name(
            config,
            primary_key="graph_neighbor_selection",
            fallback_keys=["query", "relation"],
        ),
    )
    if not neighbor_llm_settings["api_key"]:
        raise ValueError("graph llm neighbor/root selection requires api key (env OPENAI_API_KEY or llm.api_key).")
    if not neighbor_llm_settings["base_url"]:
        raise ValueError("graph llm neighbor/root selection requires base url (env OPENAI_BASE_URL or llm.base_url).")
    if not neighbor_llm_settings["model"]:
        raise ValueError("graph llm neighbor/root selection requires llm.models.graph_neighbor_selection or fallback model.")
    return neighbor_llm_settings


def _resolve_llm_model_name(
    config: dict[str, Any],
    primary_key: str,
    fallback_keys: list[str] | None = None,
) -> str:
    """Resolve a model name from llm.models with optional fallback keys."""
    llm = config.get("llm", {})
    llm_models = llm.get("models", {})
    if not isinstance(llm_models, dict):
        llm_models = {}
    model_name = str(llm_models.get(primary_key, "")).strip()
    if model_name:
        return model_name
    for key in fallback_keys or []:
        model_name = str(llm_models.get(key, "")).strip()
        if model_name:
            return model_name
    return str(llm.get("model", "")).strip()


def _build_llm_settings(llm: dict[str, Any], *, model_name: str) -> dict[str, Any]:
    """Build a common llm settings payload from config."""
    return {
        "api_key": str(llm.get("api_key", "")) or os.getenv(str(llm.get("api_key_env", "OPENAI_API_KEY")), ""),
        "base_url": str(llm.get("base_url", "")) or os.getenv(str(llm.get("base_url_env", "OPENAI_BASE_URL")), ""),
        "model": model_name,
        "temperature": float(llm.get("temperature", DEFAULT_LLM_TEMPERATURE)),
        "omit_temperature": bool(llm.get("omit_temperature", False)),
        "timeout": float(llm.get("timeout", DEFAULT_LLM_TIMEOUT)),
        "max_retries": int(llm.get("max_retries", DEFAULT_LLM_MAX_RETRIES)),
        "concurrency": max(1, int(llm.get("concurrency", 8))),
        "backoff_seconds": float(llm.get("backoff_seconds", DEFAULT_LLM_BACKOFF_SECONDS)),
        "record_return_info": bool(llm.get("record_return_info", False)),
        "return_info_dir": llm.get("return_info_dir") or "",
        "run_tag": llm.get("run_tag") or "",
        "cache_enabled": bool(llm.get("cache_enabled", True)),
        "cache_dir": llm.get("cache_dir") or "",
        "disable_env_proxy": bool(llm.get("disable_env_proxy", True)),
    }


def _resolve_relation_concurrency(llm_settings: dict[str, Any] | None) -> int:
    """Resolve per-edge relation concurrency for enrichment."""
    if not llm_settings:
        return 1
    try:
        return max(1, int(llm_settings.get("concurrency", 1)))
    except Exception:
        return 1


def _generate_relation_edge_safe(
    payload: dict[str, Any],
    llm_settings: dict[str, Any] | None,
    attempt_hook: Callable[[dict[str, Any]], None] | None,
) -> tuple[str, str, bool]:
    """Generate one edge relation with graceful fallback for concurrent execution."""
    try:
        relation_label, relation_explanation = generate_relation(
            payload,
            mode="llm",
            llm_settings=llm_settings,
            attempt_hook=attempt_hook,
        )
        return relation_label or "other", relation_explanation or "llm relation generation", False
    except Exception as exc:
        return "other", f"fallback_on_error: {exc}", True


def _relation_edge_completed(edge: dict[str, Any], mode: str) -> bool:
    """Return whether one edge already has a successful relation annotation for the requested mode."""
    if str(edge.get("relation_mode", "")).strip().lower() != str(mode).strip().lower():
        return False
    if not str(edge.get("relation_label", "")).strip():
        return False
    explanation = str(edge.get("relation_explanation", "")).strip()
    if not explanation:
        return False
    if explanation.startswith("fallback_on_error:"):
        return False
    return True


def _count_completed_relation_edges(edges: list[dict[str, Any]], mode: str) -> int:
    """Count relation-completed edges for the requested mode."""
    return sum(1 for edge in edges if _relation_edge_completed(edge, mode))
