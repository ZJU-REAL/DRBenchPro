"""FastAPI service for DRBenchPro Local Research Framework runs."""

from __future__ import annotations

import copy
import hmac
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import anyio.to_thread
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .config import CANONICAL_PROTOCOL, ResearchServicePolicy
from .engine import run_research_request
from .persistence import RunPersistence, mark_stale_running_checkpoints_interrupted
from .wikipedia.constants import INDEX_ID, SNAPSHOT_ID
from .wiki_client import WikiClientConfig, WikiToolClient
from .version import FRAMEWORK_VERSION


_SAFE_CASE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SECRET_KEYS = {
    "api_key",
    "token",
    "access_token",
    "authorization",
    "proxy_authorization",
    "x_api_key",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_case_id(value: object) -> str:
    cleaned = _SAFE_CASE_RE.sub("_", str(value or "case")).strip("._-")
    return (cleaned or "case")[:100]


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            result[str(key)] = (
                "[REDACTED]" if normalized in _SECRET_KEYS else _redact_secrets(nested)
            )
        return result
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _probe_run_dir_writable(run_root: Path) -> tuple[bool, str | None]:
    """Perform an actual write/replace/delete probe on the persistence volume."""

    probe_id = uuid.uuid4().hex
    probe = run_root / f".write-probe-{probe_id}"
    replaced = run_root / f".write-probe-{probe_id}.replaced"
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        with probe.open("x", encoding="utf-8") as output:
            output.write("ok\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(probe, replaced)
        replaced.unlink()
        return True, None
    except OSError as exc:
        for path in (probe, replaced):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False, f"{type(exc).__name__}: {exc}"


def _storage_status(
    run_root: Path,
    *,
    minimum_free_bytes: int,
    require_mount: bool,
) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(run_root)
        free_bytes = int(usage.free)
        total_bytes = int(usage.total)
    except OSError as exc:
        return {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
            "free_bytes": None,
            "total_bytes": None,
            "minimum_free_bytes": minimum_free_bytes,
            "mount_required": require_mount,
            "mount_verified": False,
        }
    mount_verified = _path_has_mount(run_root)
    return {
        "ready": free_bytes >= minimum_free_bytes and (
            mount_verified or not require_mount
        ),
        "error": None,
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "minimum_free_bytes": minimum_free_bytes,
        "free_space_sufficient": free_bytes >= minimum_free_bytes,
        "mount_required": require_mount,
        "mount_verified": mount_verified,
    }


def _path_has_mount(path: Path) -> bool:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if os.path.ismount(candidate):
            # The filesystem root alone does not prove a container data volume.
            return candidate != Path(candidate.anchor)
    return False


def _probe_wiki_manifest(
    wiki_base_url: str,
    *,
    allow_incomplete_index: bool,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Probe the fixed manifest, not merely the Wikipedia process socket."""

    if not str(wiki_base_url or "").strip():
        return False, None, "Wikipedia manifest endpoint is not configured"
    try:
        client = WikiToolClient(
            WikiClientConfig(
                base_url=str(wiki_base_url).strip(),
                snapshot_id=SNAPSHOT_ID,
                index_id=INDEX_ID,
                allow_incomplete_index=allow_incomplete_index,
                timeout_seconds=2.0,
                max_retries=0,
                disable_env_proxy=True,
            )
        )
        manifest = client.validate_index()
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    return True, manifest, None


def create_app(
    *,
    run_dir: Path,
    service_api_key: str = "",
    wiki_base_url: str = "",
    strict_policy: bool = False,
    allowed_model_profiles: Iterable[object] | None = None,
    allowed_model_routes: Iterable[dict[str, Any]] | None = None,
    allow_incomplete_index: bool = False,
    sync_thread_capacity: int = 64,
    minimum_free_bytes: int = 5 * 1024**3,
    require_persistent_mount: bool = False,
) -> FastAPI:
    """Create a service that persists each run outside the container image."""

    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    expected_key = str(service_api_key or "")
    wiki_override = str(wiki_base_url or "").strip()
    policy = ResearchServicePolicy.from_values(
        strict=strict_policy,
        allowed_model_profiles=allowed_model_profiles,
        allowed_model_routes=allowed_model_routes,
        allow_incomplete_index=allow_incomplete_index,
    )
    policy.validate_startup(
        service_api_key=expected_key,
        wiki_base_url=wiki_override,
    )
    sync_capacity = max(1, min(int(sync_thread_capacity), 512))
    minimum_free = max(0, int(minimum_free_bytes))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        mark_stale_running_checkpoints_interrupted(run_root)
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous_capacity = limiter.total_tokens
        limiter.total_tokens = sync_capacity
        try:
            yield
        finally:
            limiter.total_tokens = previous_capacity

    app = FastAPI(
        title="DRBenchPro Local Research Service",
        version=FRAMEWORK_VERSION,
        description=(
            "DRBenchPro Local Research Framework over a fixed local snapshot."
        ),
        lifespan=lifespan,
    )

    def authorize(authorization: str | None) -> None:
        if not expected_key:
            return
        supplied = ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, expected_key):
            raise HTTPException(status_code=401, detail="invalid service credential")

    @app.get("/health")
    def health() -> JSONResponse:
        writable, write_error = _probe_run_dir_writable(run_root)
        storage = _storage_status(
            run_root,
            minimum_free_bytes=minimum_free,
            require_mount=require_persistent_mount,
        )
        is_ready = writable and storage["ready"]
        payload = {
            "status": "ok" if is_ready else "not_ready",
            "ready": is_ready,
            "protocols": [CANONICAL_PROTOCOL],
            "framework_version": FRAMEWORK_VERSION,
            "run_persistence": True,
            "run_dir_writable": writable,
            "run_dir_error": write_error,
            "run_storage": storage,
            "auth_required": bool(expected_key),
            "strict_policy": policy.strict,
            "allow_incomplete_index": policy.allow_incomplete_index,
            "sync_thread_capacity": sync_capacity,
        }
        return JSONResponse(status_code=200 if is_ready else 503, content=payload)

    @app.get("/ready")
    def ready() -> JSONResponse:
        writable, write_error = _probe_run_dir_writable(run_root)
        storage = _storage_status(
            run_root,
            minimum_free_bytes=minimum_free,
            require_mount=require_persistent_mount,
        )
        wiki_ready, wiki_manifest, wiki_error = _probe_wiki_manifest(
            wiki_override,
            allow_incomplete_index=policy.allow_incomplete_index,
        )
        is_ready = writable and storage["ready"] and wiki_ready
        payload = {
            "status": "ok" if is_ready else "not_ready",
            "ready": is_ready,
            "protocols": [CANONICAL_PROTOCOL],
            "framework_version": FRAMEWORK_VERSION,
            "run_persistence": True,
            "run_dir_writable": writable,
            "run_dir_error": write_error,
            "run_storage": storage,
            "wiki_manifest_ready": wiki_ready,
            "wiki_manifest_error": wiki_error,
            "wiki_manifest": wiki_manifest,
            "expected_snapshot_id": SNAPSHOT_ID,
            "expected_index_id": INDEX_ID,
            "auth_required": bool(expected_key),
            "strict_policy": policy.strict,
            "allow_incomplete_index": policy.allow_incomplete_index,
            "sync_thread_capacity": sync_capacity,
        }
        return JSONResponse(status_code=200 if is_ready else 503, content=payload)

    @app.post("/v1/research")
    def research(
        payload: dict[str, Any] = Body(...),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        request_payload = copy.deepcopy(payload)
        try:
            policy.validate_payload(request_payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if wiki_override:
            wiki = request_payload.get("wiki")
            if not isinstance(wiki, dict):
                wiki = {}
                request_payload["wiki"] = wiki
            wiki["base_url"] = wiki_override
        if policy.strict:
            wiki = request_payload.setdefault("wiki", {})
            assert isinstance(wiki, dict)
            wiki["disable_env_proxy"] = True
        wiki = request_payload.setdefault("wiki", {})
        if isinstance(wiki, dict):
            wiki["allow_incomplete_index"] = policy.allow_incomplete_index

        writable, _write_error = _probe_run_dir_writable(run_root)
        storage = _storage_status(
            run_root,
            minimum_free_bytes=minimum_free,
            require_mount=require_persistent_mount,
        )
        if not writable or not storage["ready"]:
            raise HTTPException(
                status_code=503,
                detail=(
                    "research run persistence directory is not writable or does not "
                    "meet persistent-storage/free-space requirements"
                ),
            )

        started_at = datetime.now(timezone.utc)
        case_id = _safe_case_id(request_payload.get("case_id"))
        service_run_id = (
            started_at.strftime("%Y%m%dT%H%M%S.%fZ")
            + "_"
            + case_id
            + "_"
            + uuid.uuid4().hex[:12]
        )
        current_run_dir = run_root / service_run_id
        persistence = RunPersistence(
            current_run_dir,
            service_run_id=service_run_id,
            case_id=case_id,
            started_at_utc=started_at.isoformat(),
        )
        try:
            _atomic_write_json(
                current_run_dir / "request.json",
                {
                    "service_run_id": service_run_id,
                    "started_at_utc": started_at.isoformat(),
                    "request": _redact_secrets(request_payload),
                },
            )
            persistence.start()
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="research run persistence directory became unavailable",
            ) from exc
        try:
            response = run_research_request(
                request_payload,
                telemetry_observer=lambda record_type, record: persistence.observe(
                    record_type, _redact_secrets(record)
                ),
            )
        except (TypeError, ValueError) as exc:
            failure = {
                "service_run_id": service_run_id,
                "status": "invalid_request",
                "finished_at_utc": _utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            _atomic_write_json(current_run_dir / "error.json", failure)
            persistence.finalize("error", error=failure)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            failure = {
                "service_run_id": service_run_id,
                "status": "service_error",
                "finished_at_utc": _utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            _atomic_write_json(current_run_dir / "error.json", failure)
            persistence.finalize("error", error=failure)
            raise HTTPException(status_code=500, detail="research service failure") from exc

        metadata = response.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["service_run_id"] = service_run_id
            metadata["persisted_at_utc"] = _utc_now()
        _atomic_write_json(current_run_dir / "response.json", _redact_secrets(response))
        _atomic_write_text(
            current_run_dir / "final_report.md",
            str(response.get("final_report", "") or ""),
        )
        response_status = str(response.get("status", "") or "").strip().lower()
        terminal_status = (
            "completed"
            if response_status in {"success", "completed", "ok", "truncated"}
            else "error"
        )
        persistence.finalize(
            terminal_status,
            error=response.get("error") if terminal_status == "error" else None,
            response_status=response_status,
        )
        if terminal_status == "completed":
            _atomic_write_json(
                current_run_dir / "completed.json",
                {
                    "service_run_id": service_run_id,
                    "engine_run_id": (
                        metadata.get("run_id") if isinstance(metadata, dict) else None
                    ),
                    "status": response.get("status"),
                    "finished_at_utc": _utc_now(),
                },
            )
        return response

    return app
