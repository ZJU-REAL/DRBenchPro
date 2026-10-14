"""Materialize evidence-rich edge inputs for sampled subgraph rubric generation."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import re
from typing import Any, Callable

from drbenchpro.construction.graph.wikipedia_client import WikipediaClient, WikipediaRequestError
from drbenchpro.construction.graph.wikipedia_parser import ParsedAnchor, ParsedEntityPage, WikipediaParser
from drbenchpro.common.ids import make_ordered_edge_id, make_ordered_rubric_id, make_ordered_rubric_input_id
from drbenchpro.common.io import utc_now_iso
from drbenchpro.common.text import normalize_whitespace, normalize_wikipedia_title, safe_snippet


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
WEAK_RELATION_EVIDENCE_SOURCES = {
    "source_page_summary_fallback",
    "summary_fallback",
}
RECOVERED_RELATION_EVIDENCE_SOURCE = "query_materials_edge_context"


@dataclass
class EdgeRubricMaterializationConfig:
    """Configuration for edge-level rubric materialization from sampled subgraphs."""

    language: str = "en"
    timeout: int = 15
    max_retries: int = 0
    user_agent: str = "DRBenchProBot/0.1 (subgraph rubric materialization)"
    supplement_anchor_target_count: int = 50
    include_supplemental_tables: bool = True
    max_summary_paragraphs: int = 2
    max_summary_chars: int = 700
    max_evidence_sentence_chars: int = 400
    max_evidence_paragraph_chars: int = 900


@dataclass
class _PageMaterial:
    requested_title: str
    final_title: str
    url: str
    intro_paragraphs: list[str]
    summary: str
    anchors: list[ParsedAnchor]
    fetch_error: str = ""
    fetch_attempt_count: int = 0


class _PageMaterializer:
    """Fetch and parse Wikipedia pages once per title for rubric materialization."""

    def __init__(self, cfg: EdgeRubricMaterializationConfig):
        self.cfg = cfg
        self.client = WikipediaClient(
            language=cfg.language,
            timeout=cfg.timeout,
            max_retries=cfg.max_retries,
            user_agent=cfg.user_agent,
        )
        self.parser = WikipediaParser(language=cfg.language)
        self._cache: dict[str, _PageMaterial] = {}
        self._fetch_records: list[dict[str, Any]] = []

    def get(self, title: str) -> _PageMaterial:
        normalized = normalize_wikipedia_title(title)
        if normalized in self._cache:
            return self._cache[normalized]

        try:
            page = self.client.fetch_page(normalized)
            parsed = self.parser.parse_page(
                html=page.html,
                source_title=page.final_title,
                source_url=page.url,
                supplement_anchor_target_count=self.cfg.supplement_anchor_target_count,
                include_supplemental_tables=self.cfg.include_supplemental_tables,
            )
            intro_paragraphs = [normalize_whitespace(p) for p in parsed.text[: self.cfg.max_summary_paragraphs] if normalize_whitespace(p)]
            summary = safe_snippet(" ".join(intro_paragraphs), max_chars=self.cfg.max_summary_chars)
            material = _PageMaterial(
                requested_title=normalized,
                final_title=parsed.wikipedia_title,
                url=parsed.url,
                intro_paragraphs=intro_paragraphs,
                summary=summary,
                anchors=list(parsed.anchors),
                fetch_error="",
                fetch_attempt_count=page.attempt_count,
            )
            self._fetch_records.append(
                {
                    "requested_title": page.requested_title,
                    "final_title": parsed.wikipedia_title,
                    "attempt_count": page.attempt_count,
                    "success": True,
                    "error": "",
                }
            )
        except WikipediaRequestError as exc:
            material = _PageMaterial(
                requested_title=normalized,
                final_title=normalized,
                url="",
                intro_paragraphs=[],
                summary="",
                anchors=[],
                fetch_error=str(exc),
                fetch_attempt_count=int(getattr(exc, "attempt_count", 0) or 0),
            )
            self._fetch_records.append(
                {
                    "requested_title": normalized,
                    "final_title": normalized,
                    "attempt_count": material.fetch_attempt_count,
                    "success": False,
                    "error": str(exc),
                }
            )

        self._cache[normalized] = material
        self._cache[material.final_title] = material
        return material

    def fetch_records(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._fetch_records]

    def fetch_attempt_stats(self) -> dict[str, Any]:
        records = self.fetch_records()
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


def _anchor_order_key(anchor: ParsedAnchor) -> tuple[int, int, int, str]:
    return (
        int(anchor.paragraph_id),
        int(anchor.start),
        int(anchor.end),
        str(anchor.wikipedia_title),
    )


def _extract_sentence(paragraph: str, hints: list[str], max_chars: int) -> str:
    text = normalize_whitespace(paragraph)
    if not text:
        return ""
    sentences = [normalize_whitespace(item) for item in _SENTENCE_SPLIT_RE.split(text) if normalize_whitespace(item)]
    lowered_hints = [normalize_whitespace(item).casefold() for item in hints if normalize_whitespace(item)]
    for sentence in sentences:
        lowered_sentence = sentence.casefold()
        if any(hint in lowered_sentence for hint in lowered_hints):
            return safe_snippet(sentence, max_chars=max_chars)
    if sentences:
        return safe_snippet(sentences[0], max_chars=max_chars)
    return safe_snippet(text, max_chars=max_chars)


def _saved_query_edges(
    selected_subgraphs_payload: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    saved: dict[tuple[str, str], dict[str, Any]] = {}
    for subgraph in selected_subgraphs_payload.get("selected_subgraphs", []) or []:
        if not isinstance(subgraph, dict):
            continue
        subgraph_id = str(subgraph.get("subgraph_id", "")).strip()
        query_materials = subgraph.get("query_materials", {})
        if not isinstance(query_materials, dict):
            continue
        for edge in query_materials.get("edges", []) or []:
            if not isinstance(edge, dict):
                continue
            edge_id = str(edge.get("edge_id", "")).strip()
            if subgraph_id and edge_id:
                saved[(subgraph_id, edge_id)] = edge
    return saved


def _evidence_from_saved_query_edge(
    edge: dict[str, Any],
    source_title: str,
    target_title: str,
    cfg: EdgeRubricMaterializationConfig,
) -> dict[str, Any] | None:
    saved_source = str(edge.get("source_title", "")).strip()
    saved_target = str(edge.get("target_title", "")).strip()
    if (
        normalize_wikipedia_title(saved_source) != normalize_wikipedia_title(source_title)
        or normalize_wikipedia_title(saved_target) != normalize_wikipedia_title(target_title)
    ):
        return None

    anchor_text = normalize_whitespace(str(edge.get("anchor_text", "")))
    context_text = normalize_whitespace(str(edge.get("context_text", "")))
    if not anchor_text or not context_text:
        return None

    sentence = _extract_sentence(
        context_text,
        [anchor_text, target_title],
        max_chars=cfg.max_evidence_sentence_chars,
    )
    normalized_hints = [
        normalize_whitespace(value).casefold()
        for value in (anchor_text, target_title)
        if normalize_whitespace(value)
    ]
    if sentence and normalized_hints and not any(
        hint in sentence.casefold() for hint in normalized_hints
    ):
        sentence = safe_snippet(
            context_text, max_chars=cfg.max_evidence_sentence_chars
        )

    return {
        "relation_evidence_sentence": sentence,
        "relation_evidence_paragraph": safe_snippet(
            context_text, max_chars=cfg.max_evidence_paragraph_chars
        ),
        "relation_evidence_source": RECOVERED_RELATION_EVIDENCE_SOURCE,
        "relation_evidence_anchor_text": anchor_text,
        "relation_evidence_page_title": source_title,
        "evidence_confidence": "high",
    }


def recover_weak_relation_evidence_inputs(
    selected_subgraphs_payload: dict[str, Any],
    edge_inputs_payload: dict[str, Any],
    cfg: EdgeRubricMaterializationConfig | None = None,
) -> dict[str, Any]:
    """Replace summary fallback inputs with saved graph-edge context when available."""

    cfg = cfg or EdgeRubricMaterializationConfig()
    recovered_payload = deepcopy(edge_inputs_payload)
    saved_edges = _saved_query_edges(selected_subgraphs_payload)
    recovered_count = 0
    missing_count = 0
    for group in recovered_payload.get("subgraph_edge_rubric_inputs", []) or []:
        if not isinstance(group, dict):
            continue
        subgraph_id = str(group.get("subgraph_id", "")).strip()
        for item in group.get("edge_rubric_inputs", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("relation_evidence_source", "")) not in WEAK_RELATION_EVIDENCE_SOURCES:
                continue
            edge_id = str(item.get("edge_id", "")).strip()
            evidence = _evidence_from_saved_query_edge(
                saved_edges.get((subgraph_id, edge_id), {}),
                source_title=str(item.get("source_title", "")).strip(),
                target_title=str(item.get("target_title", "")).strip(),
                cfg=cfg,
            )
            if evidence is None:
                missing_count += 1
                continue
            item.update(evidence)
            recovered_count += 1

    metadata = recovered_payload.setdefault("metadata", {})
    metadata["query_materials_evidence_recovery"] = {
        "recovered_count": recovered_count,
        "missing_count": missing_count,
        "recovered_source": RECOVERED_RELATION_EVIDENCE_SOURCE,
        "replaced_sources": sorted(WEAK_RELATION_EVIDENCE_SOURCES),
    }
    return recovered_payload


def _find_relation_evidence(
    source_material: _PageMaterial,
    target_material: _PageMaterial,
    source_title: str,
    target_title: str,
    cfg: EdgeRubricMaterializationConfig,
) -> dict[str, Any]:
    source_target = normalize_wikipedia_title(target_material.final_title or target_title)
    target_source = normalize_wikipedia_title(source_material.final_title or source_title)

    source_matches = sorted(
        [anchor for anchor in source_material.anchors if normalize_wikipedia_title(anchor.wikipedia_title) == source_target],
        key=_anchor_order_key,
    )
    if source_matches:
        anchor = source_matches[0]
        paragraph = normalize_whitespace(anchor.context_text)
        sentence = _extract_sentence(
            paragraph,
            [anchor.text, target_title, target_material.final_title],
            max_chars=cfg.max_evidence_sentence_chars,
        )
        return {
            "relation_evidence_sentence": sentence,
            "relation_evidence_paragraph": safe_snippet(paragraph, max_chars=cfg.max_evidence_paragraph_chars),
            "relation_evidence_source": "source_page_anchor",
            "relation_evidence_anchor_text": anchor.text,
            "relation_evidence_page_title": source_material.final_title or source_title,
            "evidence_confidence": "high" if sentence else "medium",
        }

    target_matches = sorted(
        [anchor for anchor in target_material.anchors if normalize_wikipedia_title(anchor.wikipedia_title) == target_source],
        key=_anchor_order_key,
    )
    if target_matches:
        anchor = target_matches[0]
        paragraph = normalize_whitespace(anchor.context_text)
        sentence = _extract_sentence(
            paragraph,
            [anchor.text, source_title, source_material.final_title],
            max_chars=cfg.max_evidence_sentence_chars,
        )
        return {
            "relation_evidence_sentence": sentence,
            "relation_evidence_paragraph": safe_snippet(paragraph, max_chars=cfg.max_evidence_paragraph_chars),
            "relation_evidence_source": "target_page_backlink",
            "relation_evidence_anchor_text": anchor.text,
            "relation_evidence_page_title": target_material.final_title or target_title,
            "evidence_confidence": "medium",
        }

    for paragraph in source_material.intro_paragraphs:
        sentence = _extract_sentence(
            paragraph,
            [target_title, target_material.final_title],
            max_chars=cfg.max_evidence_sentence_chars,
        )
        if sentence and any(hint.casefold() in sentence.casefold() for hint in [target_title, target_material.final_title] if hint):
            return {
                "relation_evidence_sentence": sentence,
                "relation_evidence_paragraph": safe_snippet(paragraph, max_chars=cfg.max_evidence_paragraph_chars),
                "relation_evidence_source": "source_page_summary_fallback",
                "relation_evidence_anchor_text": "",
                "relation_evidence_page_title": source_material.final_title or source_title,
                "evidence_confidence": "low",
            }

    fallback_sentence = source_material.summary or target_material.summary
    return {
        "relation_evidence_sentence": safe_snippet(fallback_sentence, max_chars=cfg.max_evidence_sentence_chars),
        "relation_evidence_paragraph": safe_snippet(fallback_sentence, max_chars=cfg.max_evidence_paragraph_chars),
        "relation_evidence_source": "summary_fallback",
        "relation_evidence_anchor_text": "",
        "relation_evidence_page_title": source_material.final_title or source_title,
        "evidence_confidence": "low",
    }


def materialize_subgraph_edge_rubric_inputs(
    selected_subgraphs_payload: dict[str, Any],
    cfg: EdgeRubricMaterializationConfig,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build evidence-rich edge inputs for every selected subgraph edge."""
    metadata = selected_subgraphs_payload.get("metadata", {})
    selected_subgraphs = selected_subgraphs_payload.get("selected_subgraphs", [])
    if not isinstance(selected_subgraphs, list):
        raise ValueError("selected_subgraphs payload must contain a list at 'selected_subgraphs'")

    page_materializer = _PageMaterializer(cfg)
    saved_query_edges = _saved_query_edges(selected_subgraphs_payload)
    unique_titles: set[str] = set()
    for subgraph in selected_subgraphs:
        if not isinstance(subgraph, dict):
            continue
        for edge in subgraph.get("edges", []):
            if not isinstance(edge, dict):
                continue
            unique_titles.add(str(edge.get("source_title", "")).strip())
            unique_titles.add(str(edge.get("target_title", "")).strip())
    unique_titles.discard("")

    total_titles = len(unique_titles)
    fetched_titles = 0
    for title in sorted(unique_titles):
        page_materializer.get(title)
        fetched_titles += 1
        if progress_hook is not None:
            progress_hook(
                {
                    "stage": "page_materialized",
                    "completed": fetched_titles,
                    "total": total_titles,
                    "title": title,
                }
            )

    grouped_inputs: list[dict[str, Any]] = []
    flat_edge_count = 0
    fetch_error_count = 0
    recovered_evidence_count = 0
    missing_saved_evidence_count = 0

    for subgraph in selected_subgraphs:
        if not isinstance(subgraph, dict):
            continue
        subgraph_id = str(subgraph.get("subgraph_id", "")).strip()
        edge_inputs: list[dict[str, Any]] = []
        for edge_index, edge in enumerate(subgraph.get("edges", []), start=1):
            if not isinstance(edge, dict):
                continue
            source_title = str(edge.get("source_title", "")).strip()
            target_title = str(edge.get("target_title", "")).strip()
            source_material = page_materializer.get(source_title)
            target_material = page_materializer.get(target_title)
            evidence = _find_relation_evidence(
                source_material=source_material,
                target_material=target_material,
                source_title=source_title,
                target_title=target_title,
                cfg=cfg,
            )
            edge_id = str(edge.get("edge_id", "")).strip() or make_ordered_edge_id(edge_index)
            if str(evidence.get("relation_evidence_source", "")) in WEAK_RELATION_EVIDENCE_SOURCES:
                recovered = _evidence_from_saved_query_edge(
                    saved_query_edges.get((subgraph_id, edge_id), {}),
                    source_title=source_title,
                    target_title=target_title,
                    cfg=cfg,
                )
                if recovered is not None:
                    evidence = recovered
                    recovered_evidence_count += 1
                else:
                    missing_saved_evidence_count += 1
            rubric_id = make_ordered_rubric_id(subgraph_id, edge_id)
            edge_input = {
                "rubric_input_id": make_ordered_rubric_input_id(subgraph_id, edge_id),
                "rubric_id": rubric_id,
                "subgraph_id": subgraph_id,
                "subgraph_type": str(subgraph.get("subgraph_type", "")).strip(),
                "anchor_node": str(subgraph.get("anchor_node", "")).strip(),
                "edge_id": edge_id,
                "edge_depth": int(edge.get("edge_depth", edge.get("depth", 0)) or 0),
                "source_title": source_title,
                "target_title": target_title,
                "source_url": source_material.url,
                "target_url": target_material.url,
                "source_page_summary": source_material.summary,
                "target_page_summary": target_material.summary,
                "source_intro_paragraphs": list(source_material.intro_paragraphs),
                "target_intro_paragraphs": list(target_material.intro_paragraphs),
                "source_fetch_error": source_material.fetch_error,
                "target_fetch_error": target_material.fetch_error,
                "source_fetch_attempt_count": source_material.fetch_attempt_count,
                "target_fetch_attempt_count": target_material.fetch_attempt_count,
                **evidence,
            }
            if source_material.fetch_error or target_material.fetch_error:
                fetch_error_count += 1
            edge_inputs.append(edge_input)
            flat_edge_count += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "stage": "edge_materialized",
                        "subgraph_id": subgraph_id,
                        "edge_id": edge_id,
                        "source_title": source_title,
                        "target_title": target_title,
                    }
                )

        grouped_inputs.append(
            {
                "subgraph_id": subgraph_id,
                "subgraph_type": str(subgraph.get("subgraph_type", "")).strip(),
                "anchor_node": str(subgraph.get("anchor_node", "")).strip(),
                "edge_rubric_inputs": edge_inputs,
            }
        )

    return {
        "metadata": {
            "artifact_type": "subgraph_edge_rubric_inputs",
            "graph_id": str(metadata.get("graph_id", "")).strip(),
            "seed_entity": str(metadata.get("seed_entity", "")).strip(),
            "source_subgraphs_path": str(metadata.get("source_graph_path", "")).strip(),
            "build_time_utc": utc_now_iso(),
            "subgraph_count": len(grouped_inputs),
            "edge_input_count": flat_edge_count,
            "unique_page_count": total_titles,
            "page_fetch_error_count": fetch_error_count,
            "unique_page_fetch_error_count": page_materializer.fetch_attempt_stats().get("error_count", 0),
            "page_fetch_attempt_stats": page_materializer.fetch_attempt_stats(),
            "page_fetch_attempt_records": page_materializer.fetch_records(),
            "query_materials_evidence_recovery": {
                "recovered_count": recovered_evidence_count,
                "missing_count": missing_saved_evidence_count,
                "recovered_source": RECOVERED_RELATION_EVIDENCE_SOURCE,
                "replaced_sources": sorted(WEAK_RELATION_EVIDENCE_SOURCES),
            },
            "materialization_config": {
                "language": cfg.language,
                "timeout": cfg.timeout,
                "max_retries": cfg.max_retries,
                "supplement_anchor_target_count": cfg.supplement_anchor_target_count,
                "include_supplemental_tables": cfg.include_supplemental_tables,
                "max_summary_paragraphs": cfg.max_summary_paragraphs,
                "max_summary_chars": cfg.max_summary_chars,
                "max_evidence_sentence_chars": cfg.max_evidence_sentence_chars,
                "max_evidence_paragraph_chars": cfg.max_evidence_paragraph_chars,
            },
        },
        "subgraph_edge_rubric_inputs": grouped_inputs,
    }
