#!/usr/bin/env python3
"""Build or resume a fixed-snapshot SQLite/FTS5 Wikipedia index."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from drbenchpro.local_research.wikipedia.builder import BuildConfig, WikipediaIndexBuilder
from drbenchpro.local_research.wikipedia.constants import SNAPSHOT_ID


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build/resume SQLite FTS5 from a MediaWiki XML dump."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dump", type=Path)
    source.add_argument(
        "--shard-manifest",
        type=Path,
        help="download-manifest.json containing ordered XML/index shard pairs.",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--multistream-index", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--snapshot-id", default=SNAPSHOT_ID)
    parser.add_argument("--expected-dump-sha1")
    parser.add_argument(
        "--verify-source-sha1",
        action="store_true",
        help="Re-hash shard XML files before indexing (downloads are already verified).",
    )
    parser.add_argument("--namespace", type=int, default=0)
    parser.add_argument("--chunk-chars", type=int, default=2200)
    parser.add_argument("--chunk-overlap", type=int, default=200)
    parser.add_argument("--transaction-pages", type=int, default=250)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--no-mwparser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = BuildConfig(
        dump_path=args.dump,
        database_path=args.database,
        snapshot_id=args.snapshot_id,
        multistream_index_path=args.multistream_index,
        shard_manifest_path=args.shard_manifest,
        manifest_path=args.manifest,
        namespace=args.namespace,
        chunk_chars=args.chunk_chars,
        chunk_overlap=args.chunk_overlap,
        transaction_pages=args.transaction_pages,
        prefer_mwparser=not args.no_mwparser,
        max_pages=args.max_pages,
        expected_dump_sha1=args.expected_dump_sha1,
        verify_source_sha1=args.verify_source_sha1,
    )
    try:
        with WikipediaIndexBuilder(config) as builder:
            manifest = builder.build()
    except Exception as exc:
        print(f"Wikipedia index build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
