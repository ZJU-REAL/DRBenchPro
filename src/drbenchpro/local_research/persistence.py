"""Crash-resilient, append-only telemetry for one local research run."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .schemas import utc_now_iso


CHECKPOINT_SCHEMA_VERSION = 1


class RunPersistence:
    """Persist telemetry before a case finishes.

    ``events.jsonl`` is the authoritative append-only stream. ``checkpoint.json``
    is an atomically replaced index for fast status inspection.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        service_run_id: str,
        case_id: str,
        started_at_utc: str,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.service_run_id = service_run_id
        self.case_id = case_id
        self.started_at_utc = started_at_utc
        self.events_path = self.run_dir / "events.jsonl"
        self.checkpoint_path = self.run_dir / "checkpoint.json"
        self._lock = threading.Lock()
        self._record_count = 0
        self._model_call_count = 0
        self._tool_call_count = 0
        self._trace_event_count = 0
        self._provider_cost_reported_calls = 0
        self._provider_cost_missing_calls = 0
        self._provider_reported_cost_usd = 0.0
        self._finalized = False

    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.append(
            "run_state",
            {
                "status": "running",
                "service_run_id": self.service_run_id,
                "case_id": self.case_id,
                "started_at_utc": self.started_at_utc,
            },
        )

    def observe(self, record_type: str, payload: dict[str, Any]) -> None:
        self.append(record_type, payload)

    def append(self, record_type: str, payload: dict[str, Any]) -> None:
        record = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "record_index": 0,
            "recorded_at_utc": utc_now_iso(),
            "record_type": str(record_type),
            "service_run_id": self.service_run_id,
            "case_id": self.case_id,
            "payload": payload,
        }
        with self._lock:
            self._record_count += 1
            record["record_index"] = self._record_count
            self._update_counters(record_type, payload)
            self._append_jsonl(record)
            self._write_checkpoint_locked(status="running", last_record=record)

    def finalize(
        self,
        status: str,
        *,
        error: dict[str, Any] | None = None,
        response_status: str | None = None,
    ) -> None:
        terminal_status = str(status).strip().lower()
        if terminal_status not in {"completed", "error", "interrupted"}:
            raise ValueError(f"Unsupported terminal run status {status!r}.")
        payload = {
            "status": terminal_status,
            "response_status": response_status,
            "finished_at_utc": utc_now_iso(),
            "error": error,
        }
        with self._lock:
            if self._finalized:
                return
            self._record_count += 1
            record = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "record_index": self._record_count,
                "recorded_at_utc": payload["finished_at_utc"],
                "record_type": "run_state",
                "service_run_id": self.service_run_id,
                "case_id": self.case_id,
                "payload": payload,
            }
            self._append_jsonl(record)
            self._write_checkpoint_locked(
                status=terminal_status,
                last_record=record,
                error=error,
                response_status=response_status,
                finished_at_utc=payload["finished_at_utc"],
            )
            self._finalized = True

    def _update_counters(self, record_type: str, payload: dict[str, Any]) -> None:
        if record_type == "model_call":
            self._model_call_count += 1
            cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
            self._provider_cost_reported_calls += int(
                cost.get("provider_cost_reported_calls", 0) or 0
            )
            self._provider_cost_missing_calls += int(
                cost.get("provider_cost_missing_calls", 0) or 0
            )
            self._provider_reported_cost_usd += float(
                cost.get("provider_reported_cost_usd", 0.0) or 0.0
            )
        elif record_type == "tool_call":
            self._tool_call_count += 1
        elif record_type == "trace_event":
            self._trace_event_count += 1

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
        with self.events_path.open("a", encoding="utf-8") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())

    def _write_checkpoint_locked(
        self,
        *,
        status: str,
        last_record: dict[str, Any],
        error: dict[str, Any] | None = None,
        response_status: str | None = None,
        finished_at_utc: str | None = None,
    ) -> None:
        checkpoint = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "service_run_id": self.service_run_id,
            "case_id": self.case_id,
            "status": status,
            "response_status": response_status,
            "started_at_utc": self.started_at_utc,
            "updated_at_utc": last_record["recorded_at_utc"],
            "finished_at_utc": finished_at_utc,
            "events_path": self.events_path.name,
            "record_count": self._record_count,
            "model_call_count": self._model_call_count,
            "tool_call_count": self._tool_call_count,
            "trace_event_count": self._trace_event_count,
            "cost": {
                "currency": "USD",
                "provider_reported_cost_usd": round(
                    self._provider_reported_cost_usd, 12
                ),
                "provider_cost_reported_calls": self._provider_cost_reported_calls,
                "provider_cost_missing_calls": self._provider_cost_missing_calls,
                "provider_cost_complete": (
                    self._provider_cost_reported_calls > 0
                    and self._provider_cost_missing_calls == 0
                ),
                "cost_missing": self._provider_cost_missing_calls > 0,
            },
            "last_record": {
                "record_index": last_record["record_index"],
                "record_type": last_record["record_type"],
                "recorded_at_utc": last_record["recorded_at_utc"],
            },
            "error": error,
        }
        _atomic_write_json(self.checkpoint_path, checkpoint)


def mark_stale_running_checkpoints_interrupted(run_root: Path) -> int:
    """Mark runs left ``running`` by a previous service process as interrupted."""

    marked = 0
    for path in Path(run_root).glob("*/checkpoint.json"):
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(checkpoint, dict) or checkpoint.get("status") != "running":
            continue
        finished_at_utc = utc_now_iso()
        error = {
            "error_type": "ProcessInterrupted",
            "error_message": "Service restarted before this run reached a terminal state.",
        }
        record_index = int(checkpoint.get("record_count", 0) or 0) + 1
        record = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "record_index": record_index,
            "recorded_at_utc": finished_at_utc,
            "record_type": "run_state",
            "service_run_id": checkpoint.get("service_run_id"),
            "case_id": checkpoint.get("case_id"),
            "payload": {
                "status": "interrupted",
                "response_status": None,
                "finished_at_utc": finished_at_utc,
                "error": error,
            },
        }
        events_path = path.parent / str(
            checkpoint.get("events_path") or "events.jsonl"
        )
        with events_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        checkpoint["status"] = "interrupted"
        checkpoint["record_count"] = record_index
        checkpoint["finished_at_utc"] = finished_at_utc
        checkpoint["updated_at_utc"] = checkpoint["finished_at_utc"]
        checkpoint["last_record"] = {
            "record_index": record_index,
            "record_type": "run_state",
            "recorded_at_utc": finished_at_utc,
        }
        checkpoint["error"] = error
        _atomic_write_json(path, checkpoint)
        marked += 1
    return marked


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
