#!/usr/bin/env python3
"""Validate runtime health and print its immutable manifest."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


from drbenchpro.local_research.wikipedia.constants import INDEX_ID, OFFICIAL_SHARD_COUNT, SNAPSHOT_ID
from drbenchpro.local_research.wikipedia.store import WikipediaStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a completed Wikipedia runtime DB.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Sidecar manifest; defaults to <database stem>.manifest.json.",
    )
    parser.add_argument("--snapshot-id", default=SNAPSHOT_ID)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--smoke-query", default="Wikipedia")
    parser.add_argument("--skip-sqlite-integrity", action="store_true")
    return parser.parse_args()


def verify_runtime(
    database: Path,
    *,
    manifest_path: Path | None = None,
    snapshot_id: str = SNAPSHOT_ID,
    allow_incomplete: bool = False,
    smoke_query: str = "Wikipedia",
    run_sqlite_integrity: bool = True,
) -> dict[str, object]:
    database = Path(database).resolve()
    sidecar_path = (
        Path(manifest_path).resolve()
        if manifest_path is not None
        else database.with_suffix(".manifest.json")
    )
    wal_path = database.with_name(database.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    if not allow_incomplete and wal_bytes:
        raise RuntimeError(f"formal runtime has a non-empty WAL: {wal_path}")

    store = WikipediaStore(
        database,
        expected_snapshot_id=snapshot_id,
        allow_incomplete=allow_incomplete,
    )
    health = store.health()
    embedded_manifest = store.manifest()
    if not sidecar_path.is_file():
        raise FileNotFoundError(sidecar_path)
    sidecar_manifest = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if sidecar_manifest != embedded_manifest:
        raise RuntimeError("sidecar manifest differs from embedded manifest_json")

    uri = database.as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=120.0) as connection:
        connection.execute("PRAGMA query_only=ON")
        quick_check = []
        foreign_key_violations = []
        if run_sqlite_integrity:
            quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            if quick_check != ["ok"]:
                raise RuntimeError(f"SQLite quick_check failed: {quick_check[:10]!r}")
            foreign_key_violations = [
                tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
            ]
            if foreign_key_violations:
                raise RuntimeError(
                    "SQLite foreign_key_check found violations: "
                    f"{foreign_key_violations[:10]!r}"
                )
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("pages", "redirects", "sections", "chunks")
        }
        fts_visible_rows = int(
            connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        )
        # chunks_fts is an external-content FTS5 table. A plain COUNT(*) reads
        # through the content table and can still match after the inverted
        # index has been deleted, while docsize contains one row per indexed
        # document.
        fts_indexed_rows = int(
            connection.execute("SELECT COUNT(*) FROM chunks_fts_docsize").fetchone()[0]
        )
        if (
            fts_visible_rows != counts["chunks"]
            or fts_indexed_rows != counts["chunks"]
        ):
            raise RuntimeError(
                "FTS row count mismatch: "
                f"visible={fts_visible_rows}, indexed={fts_indexed_rows}, "
                f"chunks={counts['chunks']}"
            )
        source_count, completed_sources = connection.execute(
            "SELECT COUNT(*), SUM(build_status='complete') FROM source_shards"
        ).fetchone()
        source_count = int(source_count or 0)
        completed_sources = int(completed_sources or 0)

    if not allow_incomplete:
        source = embedded_manifest.get("source", {})
        if (
            health.build_status != "complete"
            or not health.complete_snapshot
            or health.index_id != INDEX_ID
            or source.get("complete_snapshot") is not True
            or int(source.get("selected_shard_count", 0)) != OFFICIAL_SHARD_COUNT
            or source_count != OFFICIAL_SHARD_COUNT
            or completed_sources != OFFICIAL_SHARD_COUNT
        ):
            raise RuntimeError("formal runtime does not contain 71 completed canonical shards")

    search = store.search(smoke_query, top_k=5)
    if not search.results:
        raise RuntimeError(f"search smoke returned no results for {smoke_query!r}")
    source_id = search.results[0].source_id
    opened = store.open(source_id, cursor=0, max_chars=4_000)
    pattern_match = re.search(r"[^\W\d_]{4,}", opened.content, flags=re.UNICODE)
    if pattern_match is None:
        raise RuntimeError("open smoke returned no usable find pattern")
    pattern = pattern_match.group(0)
    found = store.find(source_id, pattern, max_results=5)
    if not found.matches:
        raise RuntimeError("find smoke did not locate text returned by open")

    return {
        "health": health.model_dump(),
        "manifest": embedded_manifest,
        "sidecar_manifest_path": str(sidecar_path),
        "sqlite": {
            "quick_check": quick_check,
            "foreign_key_violation_count": len(foreign_key_violations),
            "counts": counts,
            "fts_chunks": fts_indexed_rows,
            "fts_visible_rows": fts_visible_rows,
            "fts_indexed_rows": fts_indexed_rows,
            "source_shards": source_count,
            "completed_source_shards": completed_sources,
            "wal_bytes": wal_bytes,
        },
        "tool_smoke": {
            "query": smoke_query,
            "search_results": len(search.results),
            "source_id": source_id,
            "opened_chars": len(opened.content),
            "find_pattern": pattern,
            "find_matches": len(found.matches),
        },
    }


def main() -> int:
    args = parse_args()
    try:
        payload = verify_runtime(
            args.database,
            manifest_path=args.manifest,
            snapshot_id=args.snapshot_id,
            allow_incomplete=args.allow_incomplete,
            smoke_query=args.smoke_query,
            run_sqlite_integrity=not args.skip_sqlite_integrity,
        )
    except Exception as exc:
        print(f"Wikipedia verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
