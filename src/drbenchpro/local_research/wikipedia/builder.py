"""Resumable builder for a fixed Wikipedia SQLite/FTS5 runtime."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import (
    BUILDER_VERSION,
    CANONICAL_INDEX_CHUNK_CHARS,
    CANONICAL_INDEX_CHUNK_OVERLAP,
    CANONICAL_INDEX_CLEANER,
    CANONICAL_INDEX_NAMESPACE,
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP,
    DUMP_DATE,
    DUMP_FILENAME,
    INDEX_BACKEND,
    MULTISTREAM_INDEX_FILENAME,
    OFFICIAL_SHARD_COUNT,
    SCHEMA_VERSION,
    SHARD_MANIFEST_FORMAT,
    SNAPSHOT_ID,
    index_build_fingerprint,
    index_id_for_build,
)
from .identifiers import make_source_id
from .parser import (
    DumpPage,
    WikitextCleaner,
    chunk_text,
    clean_sections,
    iter_dump_pages,
    iter_multistream_ranges,
    read_multistream_block,
)
from .schema import configure_build_connection, initialize_schema


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_title(title: str) -> str:
    return " ".join(title.replace("_", " ").split()).casefold()


@dataclass(frozen=True)
class ShardSource:
    shard_id: str
    ordinal: int
    dump_path: Path
    index_path: Optional[Path]
    expected_dump_sha1: Optional[str]
    expected_index_sha1: Optional[str]


def _artifact_path(manifest_path: Path, artifact: Dict[str, Any]) -> Path:
    raw = artifact.get("path") or artifact.get("filename")
    if not raw:
        raise ValueError("shard artifact is missing path/filename")
    path = Path(str(raw))
    return path if path.is_absolute() else manifest_path.parent / path


def load_shard_sources(
    manifest_path: Path, expected_snapshot_id: str
) -> Tuple[List[ShardSource], bool]:
    """Load an ordered, portable dated-download manifest."""

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("format") != SHARD_MANIFEST_FORMAT:
        raise ValueError(f"unsupported shard manifest format: {payload.get('format')!r}")
    if payload.get("snapshot_id") != expected_snapshot_id:
        raise ValueError(
            "shard manifest snapshot mismatch: "
            f"{payload.get('snapshot_id')!r} != {expected_snapshot_id!r}"
        )
    complete_snapshot = payload.get("complete_snapshot") is True
    declared_official_count = int(
        payload.get("official_shard_count", OFFICIAL_SHARD_COUNT)
    )
    if declared_official_count != OFFICIAL_SHARD_COUNT:
        raise ValueError(
            "shard manifest official_shard_count mismatch: "
            f"{declared_official_count} != {OFFICIAL_SHARD_COUNT}"
        )
    sources: List[ShardSource] = []
    combined = payload.get("combined")
    if isinstance(combined, dict):
        dump = combined.get("dump")
        index = combined.get("index")
        if not isinstance(dump, dict) or not isinstance(index, dict):
            raise ValueError("combined manifest entries must contain dump and index")
        if complete_snapshot and (not dump.get("sha1") or not index.get("sha1")):
            raise ValueError(
                "complete combined manifest entries require dump and index SHA-1"
            )
        if complete_snapshot and (
            _artifact_path(manifest_path, dump).name != DUMP_FILENAME
            or _artifact_path(manifest_path, index).name
            != MULTISTREAM_INDEX_FILENAME
        ):
            raise ValueError(
                "complete combined manifest must use the dated official artifact names"
            )
        sources.append(
            ShardSource(
                shard_id="combined",
                ordinal=1,
                dump_path=_artifact_path(manifest_path, dump),
                index_path=_artifact_path(manifest_path, index),
                expected_dump_sha1=(str(dump["sha1"]) if dump.get("sha1") else None),
                expected_index_sha1=(
                    str(index["sha1"]) if index.get("sha1") else None
                ),
            )
        )
    else:
        shards = payload.get("shards")
        if not isinstance(shards, list) or not shards:
            raise ValueError("shard manifest has no shards")
        previous_ordinal = 0
        previous_last_page = 0
        for item in shards:
            if not isinstance(item, dict):
                raise ValueError("invalid shard manifest entry")
            ordinal = int(item["ordinal"])
            first_page = int(item["first_page_id"])
            last_page = int(item["last_page_id"])
            if ordinal != previous_ordinal + 1:
                raise ValueError("shard ordinals must be contiguous and start at 1")
            if first_page <= previous_last_page or last_page < first_page:
                raise ValueError("shard page ranges must be strictly ordered")
            dump = item.get("dump")
            index = item.get("index")
            if not isinstance(dump, dict) or not isinstance(index, dict):
                raise ValueError("each shard must contain dump and index artifacts")
            if complete_snapshot and (not dump.get("sha1") or not index.get("sha1")):
                raise ValueError(
                    "complete shard manifest entries require dump and index SHA-1"
                )
            if complete_snapshot:
                part_number = int(item["part_number"])
                expected_dump_name = (
                    f"enwiki-{DUMP_DATE}-pages-articles-multistream{part_number}"
                    f".xml-p{first_page}p{last_page}.bz2"
                )
                expected_index_name = (
                    f"enwiki-{DUMP_DATE}-pages-articles-multistream-index"
                    f"{part_number}.txt-p{first_page}p{last_page}.bz2"
                )
                if (
                    _artifact_path(manifest_path, dump).name != expected_dump_name
                    or _artifact_path(manifest_path, index).name
                    != expected_index_name
                ):
                    raise ValueError(
                        "complete shard manifest artifact names do not match their "
                        "dated part/page-range identity"
                    )
            sources.append(
                ShardSource(
                    shard_id=str(item.get("shard_id") or f"{ordinal:03d}"),
                    ordinal=ordinal,
                    dump_path=_artifact_path(manifest_path, dump),
                    index_path=_artifact_path(manifest_path, index),
                    expected_dump_sha1=(
                        str(dump["sha1"]) if dump.get("sha1") else None
                    ),
                    expected_index_sha1=(
                        str(index["sha1"]) if index.get("sha1") else None
                    ),
                )
            )
            previous_ordinal = ordinal
            previous_last_page = last_page
        selected_count = payload.get("selected_shard_count")
        if selected_count is not None and int(selected_count) != len(sources):
            raise ValueError(
                "shard manifest selected_shard_count does not match shard entries"
            )
        if complete_snapshot and len(sources) != OFFICIAL_SHARD_COUNT:
            raise ValueError(
                "complete dated snapshot requires exactly "
                f"{OFFICIAL_SHARD_COUNT} shards; observed {len(sources)}"
            )
    for source in sources:
        if not source.dump_path.is_file():
            raise FileNotFoundError(source.dump_path)
        if source.index_path and not source.index_path.is_file():
            raise FileNotFoundError(source.index_path)
    return sources, complete_snapshot


@dataclass(frozen=True)
class BuildConfig:
    database_path: Path
    dump_path: Optional[Path] = None
    snapshot_id: str = SNAPSHOT_ID
    multistream_index_path: Optional[Path] = None
    shard_manifest_path: Optional[Path] = None
    manifest_path: Optional[Path] = None
    namespace: int = 0
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    transaction_pages: int = 250
    prefer_mwparser: bool = True
    max_pages: Optional[int] = None
    expected_dump_sha1: Optional[str] = None
    verify_source_sha1: bool = False

    def validate(self) -> None:
        if bool(self.dump_path) == bool(self.shard_manifest_path):
            raise ValueError("provide exactly one of dump_path or shard_manifest_path")
        if self.dump_path and not self.dump_path.is_file():
            raise FileNotFoundError(self.dump_path)
        if self.shard_manifest_path and not self.shard_manifest_path.is_file():
            raise FileNotFoundError(self.shard_manifest_path)
        if self.shard_manifest_path and self.multistream_index_path:
            raise ValueError(
                "multistream_index_path is only valid with a single dump_path"
            )
        if self.multistream_index_path and not self.multistream_index_path.is_file():
            raise FileNotFoundError(self.multistream_index_path)
        if not self.snapshot_id or ":" in self.snapshot_id:
            raise ValueError("snapshot_id must be non-empty and cannot contain ':'")
        if self.transaction_pages < 1:
            raise ValueError("transaction_pages must be positive")
        if self.max_pages is not None and self.max_pages < 1:
            raise ValueError("max_pages must be positive")
        chunk_text("validation", self.chunk_chars, self.chunk_overlap)


class WikipediaIndexBuilder:
    """Build an immutable runtime DB with transactional checkpoints."""

    def __init__(self, config: BuildConfig) -> None:
        config.validate()
        self.config = config
        self.cleaner = WikitextCleaner(prefer_mwparser=config.prefer_mwparser)
        self.index_id = index_id_for_build(
            config.snapshot_id,
            namespace=config.namespace,
            chunk_chars=config.chunk_chars,
            chunk_overlap=config.chunk_overlap,
            cleaner=self.cleaner.backend,
        )
        self.index_build_fingerprint = index_build_fingerprint(
            namespace=config.namespace,
            chunk_chars=config.chunk_chars,
            chunk_overlap=config.chunk_overlap,
            cleaner=self.cleaner.backend,
        )
        if config.shard_manifest_path:
            self.sources, self.source_complete_snapshot = load_shard_sources(
                config.shard_manifest_path, config.snapshot_id
            )
            self.source_kind = "sharded_multistream"
        else:
            assert config.dump_path is not None
            self.sources = [
                ShardSource(
                    shard_id="single",
                    ordinal=1,
                    dump_path=config.dump_path,
                    index_path=config.multistream_index_path,
                    expected_dump_sha1=config.expected_dump_sha1,
                    expected_index_sha1=None,
                )
            ]
            # A bare XML path has no dated-manifest proof that it contains the
            # complete official snapshot. It remains useful for fixtures and
            # pilots, but can never be promoted to a formal complete index.
            self.source_complete_snapshot = False
            self.source_kind = (
                "single_multistream"
                if config.multistream_index_path
                else "single_xml"
            )
        if self.source_complete_snapshot:
            observed_build = {
                "namespace": config.namespace,
                "chunk_chars": config.chunk_chars,
                "chunk_overlap": config.chunk_overlap,
                "cleaner": self.cleaner.backend,
            }
            canonical_build = {
                "namespace": CANONICAL_INDEX_NAMESPACE,
                "chunk_chars": CANONICAL_INDEX_CHUNK_CHARS,
                "chunk_overlap": CANONICAL_INDEX_CHUNK_OVERLAP,
                "cleaner": CANONICAL_INDEX_CLEANER,
            }
            if observed_build != canonical_build:
                raise ValueError(
                    "complete snapshots require the canonical index build config: "
                    f"observed={observed_build!r}, expected={canonical_build!r}"
                )
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(config.database_path))
        self.connection.row_factory = sqlite3.Row
        configure_build_connection(self.connection)
        initialize_schema(self.connection)
        self._initialize_identity()
        self._register_sources()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "WikipediaIndexBuilder":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _metadata_get(self, key: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def _metadata_set(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _state_get_int(self, key: str, default: int) -> int:
        row = self.connection.execute(
            "SELECT value FROM build_state WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0]) if row else default

    def _state_set(self, key: str, value: object) -> None:
        self.connection.execute(
            "INSERT INTO build_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _initialize_identity(self) -> None:
        identity = {
            "snapshot_id": self.config.snapshot_id,
            "index_id": self.index_id,
            "index_build_fingerprint": self.index_build_fingerprint,
            "source_kind": self.source_kind,
            "complete_snapshot": "true" if self.source_complete_snapshot else "false",
            "schema_version": str(SCHEMA_VERSION),
            "builder_version": BUILDER_VERSION,
            "namespace": str(self.config.namespace),
            "chunk_chars": str(self.config.chunk_chars),
            "chunk_overlap": str(self.config.chunk_overlap),
            "cleaner_backend": self.cleaner.backend,
        }
        for key, expected in identity.items():
            existing = self._metadata_get(key)
            if existing is not None and existing != expected:
                raise RuntimeError(
                    f"database identity mismatch for {key}: {existing!r} != {expected!r}"
                )
            self._metadata_set(key, expected)
        if self._metadata_get("build_started_at") is None:
            self._metadata_set("build_started_at", utc_now())
        self._metadata_set("build_status", "building")
        self.connection.commit()

    def _register_sources(self) -> None:
        for source in self.sources:
            expected = (
                source.shard_id,
                source.ordinal,
                source.dump_path.name,
                source.index_path.name if source.index_path else None,
                source.dump_path.stat().st_size,
                source.expected_dump_sha1,
                source.expected_index_sha1,
            )
            existing = self.connection.execute(
                "SELECT shard_id, ordinal, dump_filename, index_filename, "
                "dump_size, dump_sha1, index_sha1 FROM source_shards WHERE shard_id = ?",
                (source.shard_id,),
            ).fetchone()
            if existing and tuple(existing) != expected:
                raise RuntimeError(
                    f"source shard identity changed for {source.shard_id}: "
                    f"{tuple(existing)!r} != {expected!r}"
                )
            if not existing:
                self.connection.execute(
                    "INSERT INTO source_shards("
                    "shard_id, ordinal, dump_filename, index_filename, dump_size, "
                    "dump_sha1, index_sha1"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    expected,
                )
        self.connection.commit()

    def verify_dump_sha1(self, source: ShardSource) -> Optional[str]:
        expected = source.expected_dump_sha1
        if not expected:
            return None
        digest = hashlib.sha1()
        with source.dump_path.open("rb") as input_file:
            for block in iter(lambda: input_file.read(8 * 1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual.casefold() != expected.casefold():
            raise RuntimeError(
                f"dump SHA-1 mismatch: expected {expected}, observed {actual}"
            )
        return actual

    def verify_index_sha1(self, source: ShardSource) -> Optional[str]:
        if source.index_path is None or not source.expected_index_sha1:
            return None
        digest = hashlib.sha1()
        with source.index_path.open("rb") as input_file:
            for block in iter(lambda: input_file.read(8 * 1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual.casefold() != source.expected_index_sha1.casefold():
            raise RuntimeError(
                "multistream index SHA-1 mismatch: expected "
                f"{source.expected_index_sha1}, observed {actual}"
            )
        return actual

    def _insert_page(self, page: DumpPage) -> bool:
        if page.namespace != self.config.namespace:
            return False
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO pages("
            "page_id, revision_id, title, normalized_title, namespace, "
            "revision_timestamp, revision_sha1, is_redirect"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                page.page_id,
                page.revision_id,
                page.title,
                normalize_title(page.title),
                page.namespace,
                page.revision_timestamp,
                page.revision_sha1,
                int(page.redirect_target is not None),
            ),
        )
        if cursor.rowcount == 0:
            existing = self.connection.execute(
                "SELECT revision_id, title FROM pages WHERE page_id = ?",
                (page.page_id,),
            ).fetchone()
            if existing and (
                int(existing["revision_id"]) != page.revision_id
                or str(existing["title"]) != page.title
            ):
                raise RuntimeError(
                    "page ID collision across dump sources: "
                    f"page_id={page.page_id}, existing_revision={existing['revision_id']}, "
                    f"new_revision={page.revision_id}"
                )
            return False
        if page.redirect_target:
            self.connection.execute(
                "INSERT INTO redirects("
                "page_id, revision_id, title, normalized_title, "
                "target_title, normalized_target_title"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    page.page_id,
                    page.revision_id,
                    page.title,
                    normalize_title(page.title),
                    page.redirect_target,
                    normalize_title(page.redirect_target),
                ),
            )
            return True

        for section in clean_sections(page.wikitext, self.cleaner):
            source_id = make_source_id(
                self.config.snapshot_id,
                page.page_id,
                page.revision_id,
                section.section_index,
            )
            content_sha256 = hashlib.sha256(section.content.encode("utf-8")).hexdigest()
            section_cursor = self.connection.execute(
                "INSERT INTO sections("
                "page_id, revision_id, section_index, heading, anchor, source_id, "
                "content, char_count, content_sha256"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    page.page_id,
                    page.revision_id,
                    section.section_index,
                    section.heading,
                    section.anchor,
                    source_id,
                    section.content,
                    len(section.content),
                    content_sha256,
                ),
            )
            section_id = int(section_cursor.lastrowid)
            for chunk in chunk_text(
                section.content,
                self.config.chunk_chars,
                self.config.chunk_overlap,
            ):
                self.connection.execute(
                    "INSERT INTO chunks("
                    "section_id, page_id, revision_id, section_index, chunk_index, "
                    "title, section, content, char_start, char_end"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        section_id,
                        page.page_id,
                        page.revision_id,
                        section.section_index,
                        chunk.chunk_index,
                        page.title,
                        section.heading,
                        chunk.content,
                        chunk.char_start,
                        chunk.char_end,
                    ),
                )
        return True

    def _process_batch(
        self,
        pages: Sequence[DumpPage],
        checkpoint_key: str,
        checkpoint_value: int,
    ) -> int:
        inserted = 0
        try:
            self.connection.execute("BEGIN")
            for page in pages:
                inserted += int(self._insert_page(page))
            self._state_set(checkpoint_key, checkpoint_value)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return inserted

    def _build_regular(self, source: ShardSource, page_limit: Optional[int]) -> Tuple[bool, int]:
        checkpoint_key = f"shard:{source.shard_id}:last_page_ordinal"
        last_ordinal = self._state_get_int(checkpoint_key, 0)
        batch: List[DumpPage] = []
        batch_last_ordinal = last_ordinal
        inserted_this_run = 0
        exhausted = True
        for ordinal, page in enumerate(iter_dump_pages(source.dump_path), start=1):
            if ordinal <= last_ordinal:
                continue
            batch.append(page)
            batch_last_ordinal = ordinal
            if len(batch) >= self.config.transaction_pages:
                inserted_this_run += self._process_batch(
                    batch, checkpoint_key, batch_last_ordinal
                )
                batch = []
                if (
                    page_limit is not None
                    and inserted_this_run >= page_limit
                ):
                    exhausted = False
                    break
        if batch:
            inserted_this_run += self._process_batch(
                batch, checkpoint_key, batch_last_ordinal
            )
        return exhausted, inserted_this_run

    def _build_multistream(
        self, source: ShardSource, page_limit: Optional[int]
    ) -> Tuple[bool, int]:
        assert source.index_path is not None
        checkpoint_key = f"shard:{source.shard_id}:last_stream_offset"
        last_offset = self._state_get_int(checkpoint_key, -1)
        dump_size = source.dump_path.stat().st_size
        inserted_this_run = 0
        exhausted = True
        with source.dump_path.open("rb") as input_file:
            for block in iter_multistream_ranges(
                source.index_path, dump_size
            ):
                if block.offset <= last_offset:
                    continue
                pages = read_multistream_block(input_file, block)
                inserted_this_run += self._process_batch(
                    pages, checkpoint_key, block.offset
                )
                if (
                    page_limit is not None
                    and inserted_this_run >= page_limit
                ):
                    exhausted = False
                    break
        return exhausted, inserted_this_run

    def _counts(self) -> Dict[str, int]:
        return {
            table: int(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("pages", "redirects", "sections", "chunks")
        }

    def manifest(self) -> Dict[str, Any]:
        metadata = {
            str(row[0]): str(row[1])
            for row in self.connection.execute("SELECT key, value FROM metadata")
        }
        source_rows = self.connection.execute(
            "SELECT shard_id, ordinal, dump_filename, index_filename, dump_size, "
            "dump_sha1, index_sha1, build_status, completed_at "
            "FROM source_shards ORDER BY ordinal"
        ).fetchall()
        return {
            "snapshot_id": self.config.snapshot_id,
            "index_id": self.index_id,
            "schema_version": SCHEMA_VERSION,
            "builder_version": BUILDER_VERSION,
            "status": metadata.get("build_status", "unknown"),
            "source": {
                "kind": self.source_kind,
                "manifest_filename": (
                    self.config.shard_manifest_path.name
                    if self.config.shard_manifest_path
                    else None
                ),
                "complete_snapshot": self.source_complete_snapshot,
                "selected_shard_count": len(self.sources),
                "shards": [dict(row) for row in source_rows],
            },
            "index": {
                "backend": INDEX_BACKEND,
                "build_fingerprint": self.index_build_fingerprint,
                "namespace": self.config.namespace,
                "cleaner": metadata.get("cleaner_backend"),
                "chunk_chars": self.config.chunk_chars,
                "chunk_overlap": self.config.chunk_overlap,
                "counts": self._counts(),
            },
            "build_started_at": metadata.get("build_started_at"),
            "build_completed_at": metadata.get("build_completed_at"),
        }

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        path = self.config.manifest_path or self.config.database_path.with_suffix(
            ".manifest.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def build(self) -> Dict[str, Any]:
        """Resume the build, returning and atomically writing its manifest."""

        inserted_this_run = 0
        selected_exhausted = True
        for source in self.sources:
            row = self.connection.execute(
                "SELECT build_status FROM source_shards WHERE shard_id = ?",
                (source.shard_id,),
            ).fetchone()
            if row and str(row[0]) == "complete":
                continue
            if self.config.verify_source_sha1 or (
                self.config.expected_dump_sha1 and source.shard_id == "single"
            ):
                self.verify_dump_sha1(source)
            if source.index_path and source.expected_index_sha1:
                self.verify_index_sha1(source)
            remaining = (
                None
                if self.config.max_pages is None
                else max(1, self.config.max_pages - inserted_this_run)
            )
            if source.index_path:
                exhausted, inserted = self._build_multistream(source, remaining)
            else:
                exhausted, inserted = self._build_regular(source, remaining)
            inserted_this_run += inserted
            if exhausted:
                self.connection.execute(
                    "UPDATE source_shards SET build_status='complete', completed_at=? "
                    "WHERE shard_id=?",
                    (utc_now(), source.shard_id),
                )
                self.connection.commit()
            else:
                selected_exhausted = False
                break
            if (
                self.config.max_pages is not None
                and inserted_this_run >= self.config.max_pages
            ):
                selected_exhausted = False
                break
        all_selected_complete = all(
            str(row[0]) == "complete"
            for row in self.connection.execute(
                "SELECT build_status FROM source_shards ORDER BY ordinal"
            )
        )
        is_complete = (
            selected_exhausted
            and all_selected_complete
            and self.source_complete_snapshot
        )
        status = "complete" if is_complete else "partial"
        self._metadata_set("build_status", status)
        if is_complete:
            self._metadata_set("build_completed_at", utc_now())
            self.connection.execute(
                "INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')"
            )
            self.connection.execute("PRAGMA optimize")
        manifest = self.manifest()
        self._metadata_set(
            "manifest_json",
            json.dumps(manifest, ensure_ascii=True, sort_keys=True),
        )
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._write_manifest(manifest)
        return manifest
