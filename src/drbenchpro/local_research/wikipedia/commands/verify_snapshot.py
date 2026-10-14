#!/usr/bin/env python3
"""Strictly verify the complete fixed Wikipedia download before indexing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from drbenchpro.local_research.wikipedia.constants import DUMP_BASE_URL
from drbenchpro.local_research.wikipedia.download import fetch_sha1sums, verify_complete_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify all 71 XML/index pairs plus page.sql and redirect.sql against "
            "the dated official SHA-1 registry."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-url", default=DUMP_BASE_URL)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--skip-file-sha1",
        action="store_true",
        help="Check structure, official SHA-1 records, paths, and sizes only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        official = fetch_sha1sums(args.base_url, timeout=args.timeout)
        result = verify_complete_snapshot(
            args.manifest,
            official,
            verify_file_sha1=not args.skip_file_sha1,
        )
    except Exception as exc:
        print(f"Wikipedia snapshot verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
