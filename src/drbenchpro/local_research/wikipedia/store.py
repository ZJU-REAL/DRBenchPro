"""Read-only SQLite/FTS5 access for search, open, and find."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .builder import normalize_title
from .constants import (
    CANONICAL_INDEX_CHUNK_CHARS,
    CANONICAL_INDEX_CHUNK_OVERLAP,
    CANONICAL_INDEX_CLEANER,
    CANONICAL_INDEX_NAMESPACE,
    INDEX_BACKEND,
    SCHEMA_VERSION,
    index_build_fingerprint,
    index_id_for_build,
)
from .identifiers import parse_source_id, wikipedia_citation_url
from .models import (
    FindMatch,
    FindResponse,
    HealthResponse,
    OpenResponse,
    SearchResponse,
    SearchResult,
)


_QUERY_TOKEN_RE = re.compile(r"[^\W_]+(?:['-][^\W_]+)*", flags=re.UNICODE)
_MAX_FTS_QUERY_TOKENS = 8
_FTS_QUERY_TIMEOUT_SECONDS = 20.0


class InvalidSourceId(ValueError):
    pass


class SourceNotFound(LookupError):
    pass


def _fts_queries(query: str) -> List[str]:
    tokens: List[str] = []
    seen = set()
    for token in _QUERY_TOKEN_RE.findall(query)[:24]:
        normalized = token.casefold()
        if normalized not in seen:
            seen.add(normalized)
            tokens.append(token)
    if not tokens:
        return []

    # Full-snapshot OR queries over common terms can scan millions of postings.
    # Keep the most distinctive-looking terms and relax conjunctively instead.
    selected_indices = sorted(
        range(len(tokens)), key=lambda index: (-len(tokens[index]), index)
    )[:_MAX_FTS_QUERY_TOKENS]
    remaining = [tokens[index] for index in sorted(selected_indices)]
    queries: List[str] = []
    while remaining:
        queries.append(
            " AND ".join(
                '"' + token.replace('"', '""') + '"' for token in remaining
            )
        )
        shortest = min(
            range(len(remaining)), key=lambda index: (len(remaining[index]), -index)
        )
        remaining.pop(shortest)
    return queries


def _snippet(content: str, query: str, max_chars: int = 320) -> str:
    if len(content) <= max_chars:
        return content
    starts = []
    folded = content.casefold()
    for token in _QUERY_TOKEN_RE.findall(query):
        position = folded.find(token.casefold())
        if position >= 0:
            starts.append(position)
    center = min(starts) if starts else 0
    start = max(0, center - max_chars // 3)
    end = min(len(content), start + max_chars)
    if start:
        boundary = content.find(" ", start, min(end, start + 48))
        if boundary >= 0:
            start = boundary + 1
    if end < len(content):
        boundary = content.rfind(" ", max(start, end - 48), end)
        if boundary >= 0:
            end = boundary
    return ("..." if start else "") + content[start:end].strip() + (
        "..." if end < len(content) else ""
    )


class WikipediaStore:
    """Open a completed Wikipedia database through short-lived read connections."""

    def __init__(
        self,
        database_path: Path,
        expected_snapshot_id: Optional[str] = None,
        allow_incomplete: bool = False,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        if not self.database_path.is_file():
            raise FileNotFoundError(self.database_path)
        self.allow_incomplete = allow_incomplete
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Wikipedia schema mismatch: expected {SCHEMA_VERSION}, observed {version}"
                )
            self.snapshot_id = self._metadata(connection, "snapshot_id")
            self.index_id = self._metadata(connection, "index_id")
            self.build_status = self._metadata(connection, "build_status")
            self.complete_snapshot = (
                self._metadata(connection, "complete_snapshot").casefold() == "true"
            )
            observed_build = {
                "namespace": int(self._metadata(connection, "namespace")),
                "chunk_chars": int(self._metadata(connection, "chunk_chars")),
                "chunk_overlap": int(self._metadata(connection, "chunk_overlap")),
                "cleaner": self._metadata(connection, "cleaner_backend"),
            }
            persisted_fingerprint = self._metadata_optional(
                connection, "index_build_fingerprint"
            )
            persisted_manifest = self._metadata_optional(connection, "manifest_json")
        self.index_build = observed_build
        expected_index_id = index_id_for_build(
            self.snapshot_id,
            namespace=observed_build["namespace"],
            chunk_chars=observed_build["chunk_chars"],
            chunk_overlap=observed_build["chunk_overlap"],
            cleaner=observed_build["cleaner"],
        )
        expected_fingerprint = index_build_fingerprint(
            namespace=observed_build["namespace"],
            chunk_chars=observed_build["chunk_chars"],
            chunk_overlap=observed_build["chunk_overlap"],
            cleaner=observed_build["cleaner"],
        )
        if self.index_id != expected_index_id:
            raise RuntimeError(
                "Wikipedia index identity mismatch: "
                f"expected {expected_index_id!r}, observed {self.index_id!r}"
            )
        if persisted_fingerprint and persisted_fingerprint != expected_fingerprint:
            raise RuntimeError(
                "Wikipedia index build fingerprint mismatch: "
                f"expected {expected_fingerprint!r}, observed "
                f"{persisted_fingerprint!r}"
            )
        canonical_build = {
            "namespace": CANONICAL_INDEX_NAMESPACE,
            "chunk_chars": CANONICAL_INDEX_CHUNK_CHARS,
            "chunk_overlap": CANONICAL_INDEX_CHUNK_OVERLAP,
            "cleaner": CANONICAL_INDEX_CLEANER,
        }
        if (
            self.build_status == "complete"
            and self.complete_snapshot
            and observed_build != canonical_build
        ):
            raise RuntimeError(
                "Wikipedia complete index build config is not canonical: "
                f"observed={observed_build!r}, expected={canonical_build!r}"
            )
        self._persisted_manifest = self._parse_persisted_manifest(
            persisted_manifest,
            observed_build=observed_build,
            expected_fingerprint=expected_fingerprint,
        )
        self.static_partial = (
            self.build_status == "partial" and self._persisted_manifest is not None
        )
        if expected_snapshot_id and self.snapshot_id != expected_snapshot_id:
            raise RuntimeError(
                "Wikipedia snapshot mismatch: "
                f"expected {expected_snapshot_id!r}, observed {self.snapshot_id!r}"
            )
        complete = self.build_status == "complete" and self.complete_snapshot
        if not complete and not (allow_incomplete and self.static_partial):
            raise RuntimeError(
                "Wikipedia database is neither a complete snapshot nor an explicit "
                "static partial index "
                f"(status={self.build_status!r}, complete_snapshot="
                f"{self.complete_snapshot!r}, manifest_present="
                f"{self._persisted_manifest is not None!r})"
            )

    def _connect(self) -> sqlite3.Connection:
        # Runtime indexes are finalized before serving and mounted read-only.
        # immutable=1 prevents SQLite WAL/shared-memory writes in the container.
        uri = f"file:{self.database_path}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    @staticmethod
    def _fts_rows(
        connection: sqlite3.Connection,
        match_query: str,
        limit: int,
    ) -> tuple[List[sqlite3.Row], bool]:
        deadline = time.monotonic() + _FTS_QUERY_TIMEOUT_SECONDS
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            10_000,
        )
        try:
            rows = connection.execute(
                "SELECT c.page_id, c.revision_id, c.section_index, c.chunk_index, "
                "c.content, c.char_start, c.char_end, p.title, s.heading, "
                "s.anchor, s.source_id, "
                "chunks_fts.rank AS rank "
                "FROM chunks_fts "
                "JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
                "JOIN sections s ON s.section_id = c.section_id "
                "JOIN pages p ON p.page_id = c.page_id "
                "WHERE chunks_fts MATCH ? "
                "AND chunks_fts.rank MATCH 'bm25(8.0,3.0,1.0)' "
                "ORDER BY chunks_fts.rank ASC, c.page_id ASC, "
                "c.section_index ASC, c.chunk_index ASC LIMIT ?",
                (match_query, limit),
            ).fetchall()
            return rows, False
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc).casefold():
                raise
            return [], True
        finally:
            connection.set_progress_handler(None, 0)

    @staticmethod
    def _metadata(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"Wikipedia metadata is missing {key!r}")
        return str(row[0])

    @staticmethod
    def _metadata_optional(
        connection: sqlite3.Connection, key: str
    ) -> Optional[str]:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def _parse_persisted_manifest(
        self,
        raw_manifest: Optional[str],
        *,
        observed_build: Dict[str, Any],
        expected_fingerprint: str,
    ) -> Optional[Dict[str, Any]]:
        if raw_manifest is None:
            return None
        try:
            manifest = json.loads(raw_manifest)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Wikipedia manifest_json is invalid JSON") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError("Wikipedia manifest_json must contain an object")
        expected_identity = {
            "snapshot_id": self.snapshot_id,
            "index_id": self.index_id,
            "status": self.build_status,
        }
        observed_identity = {
            key: manifest.get(key) for key in expected_identity
        }
        if observed_identity != expected_identity:
            raise RuntimeError(
                "Wikipedia manifest identity does not match database metadata: "
                f"observed={observed_identity!r}, expected={expected_identity!r}"
            )
        source = manifest.get("source")
        if (
            not isinstance(source, dict)
            or source.get("complete_snapshot") is not self.complete_snapshot
        ):
            raise RuntimeError(
                "Wikipedia manifest complete_snapshot does not match database metadata"
            )
        index = manifest.get("index")
        if not isinstance(index, dict):
            raise RuntimeError("Wikipedia manifest is missing index build parameters")
        manifest_build = {
            "namespace": index.get("namespace"),
            "chunk_chars": index.get("chunk_chars"),
            "chunk_overlap": index.get("chunk_overlap"),
            "cleaner": index.get("cleaner"),
        }
        if index.get("backend") != INDEX_BACKEND or manifest_build != observed_build:
            raise RuntimeError(
                "Wikipedia manifest build parameters do not match database metadata: "
                f"observed={manifest_build!r}, expected={observed_build!r}"
            )
        manifest_fingerprint = index.get("build_fingerprint")
        if manifest_fingerprint not in (None, expected_fingerprint):
            raise RuntimeError(
                "Wikipedia manifest build fingerprint does not match build parameters"
            )
        return manifest

    def manifest(self) -> Dict[str, Any]:
        if self._persisted_manifest is not None:
            return copy.deepcopy(self._persisted_manifest)
        with self._connect() as connection:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in ("pages", "redirects", "sections", "chunks")
            }
        return {
            "snapshot_id": self.snapshot_id,
            "index_id": self.index_id,
            "schema_version": SCHEMA_VERSION,
            "status": self.build_status,
            "source": {"complete_snapshot": self.complete_snapshot},
            "index": {
                "backend": INDEX_BACKEND,
                **self.index_build,
                "counts": counts,
            },
        }

    def health(self) -> HealthResponse:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
            connection.execute("SELECT rowid FROM chunks_fts LIMIT 1").fetchone()
        ready = (
            self.build_status == "complete" and self.complete_snapshot
        ) or (self.allow_incomplete and self.static_partial)
        return HealthResponse(
            status="ok" if ready else "not_ready",
            ready=ready,
            snapshot_id=self.snapshot_id,
            index_id=self.index_id,
            schema_version=SCHEMA_VERSION,
            build_status=self.build_status,
            complete_snapshot=self.complete_snapshot,
            database_path=str(self.database_path),
        )

    def _section_from_source(
        self, connection: sqlite3.Connection, source_id: str
    ) -> sqlite3.Row:
        try:
            identity = parse_source_id(source_id)
        except ValueError as exc:
            raise InvalidSourceId(str(exc)) from exc
        if identity.snapshot_id != self.snapshot_id:
            raise InvalidSourceId(
                f"source belongs to {identity.snapshot_id!r}, not {self.snapshot_id!r}"
            )
        row = connection.execute(
            "SELECT s.*, p.title FROM sections s "
            "JOIN pages p ON p.page_id = s.page_id "
            "WHERE s.source_id = ? AND s.page_id = ? AND s.revision_id = ? "
            "AND s.section_index = ?",
            (
                source_id,
                identity.page_id,
                identity.revision_id,
                identity.section_index,
            ),
        ).fetchone()
        if not row:
            raise SourceNotFound(source_id)
        return row

    @staticmethod
    def _resolve_exact_page(
        connection: sqlite3.Connection, normalized_query: str
    ) -> tuple[Optional[sqlite3.Row], str]:
        current = normalized_query
        redirect_fragment = ""
        seen = set()
        for _ in range(10):
            if current in seen:
                return None, ""
            seen.add(current)
            page = connection.execute(
                "SELECT * FROM pages WHERE normalized_title = ? "
                "ORDER BY page_id LIMIT 1",
                (current,),
            ).fetchone()
            if not page:
                return None, ""
            if not int(page["is_redirect"]):
                return page, redirect_fragment
            redirect = connection.execute(
                "SELECT target_title FROM redirects WHERE page_id = ?",
                (int(page["page_id"]),),
            ).fetchone()
            if not redirect:
                return None, ""
            target_title, separator, fragment = str(redirect[0]).partition("#")
            if separator and fragment.strip():
                redirect_fragment = fragment.strip()
            current = normalize_title(target_title) if target_title.strip() else current
        return None, ""

    @staticmethod
    def _resolve_exact_section(
        connection: sqlite3.Connection,
        page_id: int,
        redirect_fragment: str,
    ) -> Optional[sqlite3.Row]:
        rows = connection.execute(
            "SELECT s.*, p.title FROM sections s "
            "JOIN pages p ON p.page_id = s.page_id "
            "WHERE s.page_id = ? ORDER BY s.section_index",
            (page_id,),
        ).fetchall()
        if not rows:
            return None
        normalized_fragment = normalize_title(redirect_fragment)
        if normalized_fragment:
            for row in rows:
                if normalized_fragment in {
                    normalize_title(str(row["anchor"])),
                    normalize_title(str(row["heading"])),
                }:
                    return row
        return rows[0]

    def _result_from_row(
        self,
        row: sqlite3.Row,
        query: str,
        score: float,
    ) -> SearchResult:
        row_keys = set(row.keys())
        match_chunk_index = (
            int(row["chunk_index"]) if "chunk_index" in row_keys else None
        )
        match_char_start = (
            int(row["char_start"]) if "char_start" in row_keys else None
        )
        match_char_end = int(row["char_end"]) if "char_end" in row_keys else None
        return SearchResult(
            snapshot_id=self.snapshot_id,
            source_id=str(row["source_id"]),
            page_id=int(row["page_id"]),
            revision_id=int(row["revision_id"]),
            title=str(row["title"]),
            section=str(row["heading"]),
            snippet=_snippet(str(row["content"]), query),
            score=round(float(score), 10),
            match_chunk_index=match_chunk_index,
            match_char_start=match_char_start,
            match_char_end=match_char_end,
            suggested_cursor=max(0, match_char_start or 0),
            citation_url=wikipedia_citation_url(
                int(row["page_id"]),
                int(row["revision_id"]),
                str(row["anchor"]),
            ),
        )

    def search(self, query: str, top_k: int) -> SearchResponse:
        cleaned_query = " ".join(query.split())
        results: List[SearchResult] = []
        seen_sources = set()
        with self._connect() as connection:
            exact_page, redirect_fragment = self._resolve_exact_page(
                connection, normalize_title(cleaned_query)
            )
            if exact_page:
                exact_section = self._resolve_exact_section(
                    connection,
                    int(exact_page["page_id"]),
                    redirect_fragment,
                )
                if exact_section:
                    result = self._result_from_row(
                        exact_section, cleaned_query, score=1.0
                    )
                    results.append(result)
                    seen_sources.add(result.source_id)

            for match_query in _fts_queries(cleaned_query):
                if len(results) >= top_k:
                    break
                rows, timed_out = self._fts_rows(
                    connection,
                    match_query,
                    max(top_k * 8, 32),
                )
                for row in rows:
                    source_id = str(row["source_id"])
                    if source_id in seen_sources:
                        continue
                    result = self._result_from_row(
                        row, cleaned_query, score=max(0.0, -float(row["rank"]))
                    )
                    results.append(result)
                    seen_sources.add(source_id)
                    if len(results) >= top_k:
                        break
                if timed_out:
                    break
        return SearchResponse(
            snapshot_id=self.snapshot_id,
            query=cleaned_query,
            results=results[:top_k],
        )

    def open(self, source_id: str, cursor: int, max_chars: int) -> OpenResponse:
        with self._connect() as connection:
            row = self._section_from_source(connection, source_id)
        content = str(row["content"])
        if cursor > len(content):
            raise ValueError(
                f"cursor {cursor} exceeds source length {len(content)}"
            )
        end_cursor = min(len(content), cursor + max_chars)
        next_cursor = end_cursor if end_cursor < len(content) else None
        return OpenResponse(
            snapshot_id=self.snapshot_id,
            source_id=source_id,
            page_id=int(row["page_id"]),
            revision_id=int(row["revision_id"]),
            title=str(row["title"]),
            section=str(row["heading"]),
            content=content[cursor:end_cursor],
            cursor=cursor,
            end_cursor=end_cursor,
            next_cursor=next_cursor,
            total_chars=len(content),
            citation_url=wikipedia_citation_url(
                int(row["page_id"]), int(row["revision_id"]), str(row["anchor"])
            ),
        )

    def find(
        self, source_id: str, pattern: str, max_results: int
    ) -> FindResponse:
        with self._connect() as connection:
            row = self._section_from_source(connection, source_id)
        content = str(row["content"])
        matches: List[FindMatch] = []
        truncated = False
        for match in re.finditer(re.escape(pattern), content, flags=re.IGNORECASE):
            if len(matches) >= max_results:
                truncated = True
                break
            context_start = max(0, match.start() - 160)
            context_end = min(len(content), match.end() + 160)
            matches.append(
                FindMatch(
                    match_index=len(matches),
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                    context=content[context_start:context_end],
                    context_start=context_start,
                    context_end=context_end,
                )
            )
        return FindResponse(
            snapshot_id=self.snapshot_id,
            source_id=source_id,
            page_id=int(row["page_id"]),
            revision_id=int(row["revision_id"]),
            title=str(row["title"]),
            section=str(row["heading"]),
            pattern=pattern,
            matches=matches,
            truncated=truncated,
            citation_url=wikipedia_citation_url(
                int(row["page_id"]), int(row["revision_id"]), str(row["anchor"])
            ),
        )
