#!/usr/bin/env python3
"""Serve search/open/find over a completed fixed Wikipedia index."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


from drbenchpro.local_research.wikipedia.constants import SNAPSHOT_ID
from drbenchpro.local_research.wikipedia.service import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the local Wikipedia tool API.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot-id", default=SNAPSHOT_ID)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument(
        "--workers",
        type=int,
        choices=(1,),
        default=1,
        help="One process; FastAPI still serves concurrent requests via its thread pool.",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        default=os.environ.get("WIKIPEDIA_ALLOW_INCOMPLETE", "0") == "1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import uvicorn

        app = create_app(
            database_path=args.database,
            expected_snapshot_id=args.snapshot_id,
            allow_incomplete=args.allow_incomplete,
        )
    except Exception as exc:
        print(f"Wikipedia service startup failed: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
