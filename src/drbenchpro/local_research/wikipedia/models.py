"""HTTP request and response contracts for local Wikipedia tools."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .constants import (
    DEFAULT_OPEN_CHARS,
    MAX_FIND_PATTERN_CHARS,
    MAX_FIND_RESULTS,
    MAX_OPEN_CHARS,
    MAX_SEARCH_QUERY_CHARS,
    MAX_SEARCH_RESULTS,
    MAX_SOURCE_ID_CHARS,
)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS)
    top_k: int = Field(default=8, ge=1, le=MAX_SEARCH_RESULTS)


class SearchResult(BaseModel):
    snapshot_id: str
    source_id: str
    page_id: int
    revision_id: int
    title: str
    section: str
    snippet: str
    score: float
    citation_url: str
    match_chunk_index: Optional[int] = None
    match_char_start: Optional[int] = None
    match_char_end: Optional[int] = None
    suggested_cursor: int = 0


class SearchResponse(BaseModel):
    snapshot_id: str
    query: str
    results: List[SearchResult]


class OpenRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_CHARS)
    cursor: int = Field(default=0, ge=0)
    max_chars: int = Field(default=DEFAULT_OPEN_CHARS, ge=128, le=MAX_OPEN_CHARS)


class OpenResponse(BaseModel):
    snapshot_id: str
    source_id: str
    page_id: int
    revision_id: int
    title: str
    section: str
    content: str
    cursor: int
    end_cursor: int
    next_cursor: Optional[int]
    total_chars: int
    citation_url: str


class FindRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_CHARS)
    pattern: str = Field(min_length=1, max_length=MAX_FIND_PATTERN_CHARS)
    max_results: int = Field(default=10, ge=1, le=MAX_FIND_RESULTS)


class FindMatch(BaseModel):
    match_index: int
    start: int
    end: int
    text: str
    context: str
    context_start: int
    context_end: int


class FindResponse(BaseModel):
    snapshot_id: str
    source_id: str
    page_id: int
    revision_id: int
    title: str
    section: str
    pattern: str
    matches: List[FindMatch]
    truncated: bool
    citation_url: str


class HealthResponse(BaseModel):
    status: str
    ready: bool
    snapshot_id: str
    index_id: str
    schema_version: int
    build_status: str
    complete_snapshot: bool
    database_path: str


class ManifestResponse(BaseModel):
    snapshot_id: str
    index_id: str
    build_status: str
    complete_snapshot: bool
    manifest: Dict[str, Any]
