"""Validation for citations tied to sources actually returned by local tools."""

from __future__ import annotations

import re

from .schemas import CitationValidation


_SOURCE_CITATION_RE = re.compile(r"\[\[([^\[\]\n]{1,500})\]\]")


def validate_report_citations(
    report: str,
    retrieved_source_ids: list[str],
    *,
    citation_required: bool,
) -> CitationValidation:
    retrieved = _dedupe(retrieved_source_ids)
    retrieved_set = set(retrieved)
    cited = _dedupe(
        match.group(1).strip()
        for match in _SOURCE_CITATION_RE.finditer(str(report or ""))
        if match.group(1).strip()
    )
    return CitationValidation(
        cited_source_ids=cited,
        valid_source_ids=[source_id for source_id in cited if source_id in retrieved_set],
        invalid_source_ids=[source_id for source_id in cited if source_id not in retrieved_set],
        retrieved_source_ids=retrieved,
        citation_required=citation_required,
    )


def _dedupe(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
