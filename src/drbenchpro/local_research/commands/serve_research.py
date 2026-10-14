"""Launch the DRBenchPro Local Research HTTP service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the DRBenchPro Local Research Framework API."
    )
    parser.add_argument("--host", default=os.environ.get("DRBP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_int("DRBP_PORT", 8090))
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path(os.environ.get("DRBP_RUN_DIR", "/data/runs")),
    )
    parser.add_argument(
        "--wiki-base-url",
        default=os.environ.get("DRBP_WIKI_BASE_URL", "http://wiki-search:8091"),
    )
    parser.add_argument(
        "--service-api-key",
        default=os.environ.get("DRBP_SERVICE_API_KEY", ""),
        help="Optional bearer token for /v1/research; prefer DRBP_SERVICE_API_KEY.",
    )
    parser.add_argument(
        "--strict-policy",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DRBP_STRICT_POLICY", False),
    )
    parser.add_argument(
        "--allowed-model-profile",
        action="append",
        dest="allowed_model_profiles",
        default=None,
        help="Repeat for each server-approved model profile.",
    )
    parser.add_argument(
        "--sync-thread-capacity",
        type=int,
        default=_env_int("DRBP_SYNC_THREAD_CAPACITY", 64),
    )
    parser.add_argument(
        "--minimum-free-bytes",
        type=int,
        default=_env_int("DRBP_MINIMUM_FREE_BYTES", 5 * 1024**3),
    )
    parser.add_argument(
        "--require-persistent-mount",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DRBP_REQUIRE_PERSISTENT_MOUNT", False),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from drbenchpro.local_research.service import create_app

    profiles = (
        args.allowed_model_profiles
        if args.allowed_model_profiles is not None
        else _csv_env("DRBP_ALLOWED_MODEL_PROFILES")
    )
    app = create_app(
        run_dir=args.run_dir,
        service_api_key=args.service_api_key,
        wiki_base_url=args.wiki_base_url,
        strict_policy=args.strict_policy,
        allowed_model_profiles=profiles,
        allow_incomplete_index=_env_bool("DRBP_ALLOW_INCOMPLETE_INDEX", False),
        sync_thread_capacity=args.sync_thread_capacity,
        minimum_free_bytes=args.minimum_free_bytes,
        require_persistent_mount=args.require_persistent_mount,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
