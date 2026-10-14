"""Single-process commands for the image with a bundled Wikipedia index."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.request import ProxyHandler, build_opener

WIKI = Path("/opt/drbenchpro/wiki/enwiki.sqlite3")
MANIFEST = Path("/opt/drbenchpro/wiki/enwiki.manifest.json")
DATA = Path("/opt/drbenchpro/data")
CLI_COMMANDS = {"research", "evaluate", "metrics", "init-config", "preflight",
                "validate-reports", "verify-data", "validate"}


def check_index(database=WIKI, manifest=MANIFEST):
    """Check fixed metadata and sidecar without scanning the whole database."""
    from .local_research.wikipedia.constants import INDEX_ID, SNAPSHOT_ID, OFFICIAL_SHARD_COUNT
    from .local_research.wikipedia.store import WikipediaStore
    store = WikipediaStore(Path(database), expected_snapshot_id=SNAPSHOT_ID)
    embedded = store.manifest()
    if json.loads(Path(manifest).read_text()) != embedded:
        raise ValueError("Wikipedia sidecar differs from database manifest")
    source = embedded.get("source", {})
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        count, complete = connection.execute(
            "SELECT COUNT(*), SUM(build_status='complete') FROM source_shards").fetchone()
    if (store.index_id != INDEX_ID or source.get("complete_snapshot") is not True
            or source.get("selected_shard_count") != OFFICIAL_SHARD_COUNT
            or count != OFFICIAL_SHARD_COUNT or complete != OFFICIAL_SHARD_COUNT):
        raise ValueError("Bundled service requires the complete canonical 71-shard index")
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Bundled index has a nonempty WAL")
    return embedded


def healthcheck():
    """Require a live service with the same complete canonical data identity."""
    from .local_research.wikipedia.constants import INDEX_ID, SNAPSHOT_ID
    embedded = check_index()
    opener = build_opener(ProxyHandler({}))
    for route in ("health", "manifest"):
        with opener.open(f"http://127.0.0.1:8091/{route}", timeout=5) as response:
            payload = json.load(response)
        if (payload.get("snapshot_id") != SNAPSHOT_ID or payload.get("index_id") != INDEX_ID
                or payload.get("build_status") != "complete"
                or payload.get("complete_snapshot") is not True):
            raise ValueError("Wikipedia service identity or completion status differs")
        if route == "health" and payload.get("ready") is not True:
            raise ValueError("Wikipedia service is not ready")
        if route == "manifest" and payload.get("manifest") != embedded:
            raise ValueError("Serving manifest differs from bundled database")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args else "serve"
    try:
        if command in {"--help", "-h", "help"}:
            print("Usage: drbp-container [serve | " + " | ".join(sorted(CLI_COMMANDS))
                  + " | verify-wiki | research-api] [arguments]\n"
                  "Default: serve the built-in Wikipedia index on port 8091, without model calls.\n"
                  "CLI commands retain their own --help and required arguments.")
            return 0
        if command == "serve":
            if args:
                raise ValueError("serve takes no arguments; Wiki listens on 0.0.0.0:8091")
            check_index()
            os.environ.pop("WIKIPEDIA_ALLOW_INCOMPLETE", None)
            os.execvp("drbp-wiki-serve", ["drbp-wiki-serve", "--database", str(WIKI),
                       "--host", "0.0.0.0", "--port", "8091"])
        elif command == "healthcheck":
            if args:
                raise ValueError("healthcheck takes no arguments")
            healthcheck()
            return 0
        elif command == "verify-wiki":
            os.execvp(sys.executable, [sys.executable, "-m",
                       "drbenchpro.local_research.wikipedia.commands.verify_index",
                       "--database", str(WIKI), "--manifest", str(MANIFEST), *args])
        elif command == "research-api":
            os.environ.setdefault("DRBP_WIKI_BASE_URL", "http://127.0.0.1:8091")
            os.environ.setdefault("DRBP_RUN_DIR", "/outputs/api")
            os.execvp("drbp-research-serve", ["drbp-research-serve", *args])
        elif command in CLI_COMMANDS:
            if command == "verify-data" and not args:
                args = ["--registry", str(DATA / "registry.json")]
            elif command == "validate" and not args:
                args = ["--cases", str(DATA / "benchmark/cases.jsonl")]
            os.execvp(sys.executable, [sys.executable, "-m", "drbenchpro", command, *args])
        else:
            raise ValueError(f"Unknown container command: {command}; use --help")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"Container command failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
