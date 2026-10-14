#!/usr/bin/env python3
"""Download and verify the fixed enwiki-20260701 snapshot artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from drbenchpro.local_research.wikipedia.constants import DUMP_BASE_URL
from drbenchpro.local_research.wikipedia.download import download_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume and SHA-1 verify the fixed Wikipedia dump and index."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=DUMP_BASE_URL)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent file downloads (default: 4).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=20,
        help="Automatic retries per file after the initial attempt (default: 20).",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=5.0,
        help="Initial exponential retry delay, capped at 60 seconds.",
    )
    parser.add_argument(
        "--shard-limit",
        type=int,
        help="Download only the first N ordered shard pairs for a pilot.",
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Download the combined XML/index pair instead of the 71 split pairs.",
    )
    parser.add_argument(
        "--skip-supplemental",
        action="store_true",
        help="Skip page.sql/redirect.sql for an XML/index-only pilot.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    def emit_progress(event: dict[str, object]) -> None:
        print(
            json.dumps({"event": "download_progress", **event}, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )

    try:
        manifest = download_snapshot(
            output_dir=args.output_dir,
            base_url=args.base_url,
            shard_limit=args.shard_limit,
            combined=args.combined,
            include_supplemental=not args.skip_supplemental,
            workers=args.workers,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            progress=emit_progress,
        )
    except Exception as exc:
        print(f"Wikipedia download failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
