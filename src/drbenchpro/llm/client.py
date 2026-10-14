"""LLM client helpers for OpenAI-compatible chat completion APIs."""

from __future__ import annotations

import json
import os
import random
import re
import time
import hashlib
import httpx
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, get_ident
from dataclasses import dataclass
from typing import Any, Callable

from drbenchpro.common.ids import slugify


DEFAULT_LLM_TEMPERATURE = 0.2
DEFAULT_LLM_TIMEOUT = 600.0
DEFAULT_LLM_MAX_RETRIES = 20
DEFAULT_LLM_BACKOFF_SECONDS = 2.0
DEFAULT_LLM_MAX_BACKOFF_SECONDS = 10.0
DEFAULT_LLM_RATE_LIMIT_MAX_WAIT_SECONDS = 90.0
DEFAULT_LLM_DISABLE_ENV_PROXY = True

LLM_MONITOR_DIR_ENV = "DRBENCH_LLM_MONITOR_DIR"
LLM_MONITOR_RUN_ID_ENV = "DRBENCH_LLM_MONITOR_RUN_ID"
_LLM_MONITOR_LOCK = Lock()
_LLM_MONITOR_COUNTER = 0


@dataclass
class LLMConfig:
    """Runtime config for one OpenAI-compatible chat model."""

    api_key: str
    base_url: str
    model: str
    temperature: float = DEFAULT_LLM_TEMPERATURE
    omit_temperature: bool = False
    timeout: float = DEFAULT_LLM_TIMEOUT
    max_retries: int = DEFAULT_LLM_MAX_RETRIES
    backoff_seconds: float = DEFAULT_LLM_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_LLM_MAX_BACKOFF_SECONDS
    rate_limit_max_wait_seconds: float = DEFAULT_LLM_RATE_LIMIT_MAX_WAIT_SECONDS
    record_return_info: bool = False
    return_info_dir: str = ""
    run_tag: str = ""
    cache_enabled: bool = True
    cache_dir: str = ""
    disable_env_proxy: bool = DEFAULT_LLM_DISABLE_ENV_PROXY

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "LLMConfig":
        """Build LLMConfig from loosely-typed settings dict with centralized defaults."""
        if settings is None:
            settings = {}
        return cls(
            api_key=str(settings.get("api_key", "")),
            base_url=str(settings.get("base_url", "")),
            model=str(settings.get("model", "")),
            temperature=float(settings.get("temperature", DEFAULT_LLM_TEMPERATURE)),
            omit_temperature=_coerce_bool(settings.get("omit_temperature", False), default=False),
            timeout=float(settings.get("timeout", DEFAULT_LLM_TIMEOUT)),
            max_retries=max(1, int(settings.get("max_retries", DEFAULT_LLM_MAX_RETRIES))),
            backoff_seconds=max(0.0, float(settings.get("backoff_seconds", DEFAULT_LLM_BACKOFF_SECONDS))),
            max_backoff_seconds=max(0.0, float(settings.get("max_backoff_seconds", DEFAULT_LLM_MAX_BACKOFF_SECONDS))),
            rate_limit_max_wait_seconds=max(
                0.0,
                float(settings.get("rate_limit_max_wait_seconds", DEFAULT_LLM_RATE_LIMIT_MAX_WAIT_SECONDS)),
            ),
            record_return_info=bool(settings.get("record_return_info", False)),
            return_info_dir=_norm_opt_str(settings.get("return_info_dir", "")),
            run_tag=_norm_opt_str(settings.get("run_tag", "")),
            cache_enabled=bool(settings.get("cache_enabled", True)),
            cache_dir=_norm_opt_str(settings.get("cache_dir", "")),
            disable_env_proxy=_coerce_bool(
                settings.get("disable_env_proxy", DEFAULT_LLM_DISABLE_ENV_PROXY),
                default=DEFAULT_LLM_DISABLE_ENV_PROXY,
            ),
        )


class LLMReturnInfoRecorder:
    """Persist per-call LLM request/response records to JSON files."""

    def __init__(self, output_dir: str, run_tag: str = ""):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_tag = slugify(run_tag) if run_tag else "run"
        self._lock = Lock()
        self._counter = 0

    def record(self, payload: dict[str, Any]) -> str:
        """Write one call record and return file path."""
        with self._lock:
            self._counter += 1
            idx = self._counter

        task = slugify(str(payload.get("task_type", "llm")))
        context = payload.get("call_context", {}) or {}
        source = slugify(str(context.get("source_title", "")))[:32]
        target = slugify(str(context.get("target_title", "")))[:32]

        suffix_parts = [task]
        if source:
            suffix_parts.append(source)
        if target:
            suffix_parts.append(target)
        suffix = "_".join([part for part in suffix_parts if part])
        name = f"{idx:04d}_{self.run_tag}_{suffix}.json"

        payload = {
            "record_ts_utc": _utc_iso(),
            "call_index": idx,
            **payload,
        }
        path = self.output_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)


_RECORDER_CACHE: dict[tuple[str, str], LLMReturnInfoRecorder] = {}
_RECORDER_CACHE_LOCK = Lock()


def _get_recorder(output_dir: str, run_tag: str) -> LLMReturnInfoRecorder:
    key = (str(Path(output_dir).resolve()), run_tag)
    with _RECORDER_CACHE_LOCK:
        rec = _RECORDER_CACHE.get(key)
        if rec is None:
            rec = LLMReturnInfoRecorder(output_dir=output_dir, run_tag=run_tag)
            _RECORDER_CACHE[key] = rec
        return rec


def _next_monitor_attempt_id() -> str:
    global _LLM_MONITOR_COUNTER
    with _LLM_MONITOR_LOCK:
        _LLM_MONITOR_COUNTER += 1
        idx = _LLM_MONITOR_COUNTER
    return f"{os.getpid()}-{get_ident()}-{idx}"


def _emit_llm_monitor_event(event: dict[str, Any]) -> None:
    """Best-effort cross-process LLM telemetry for live pipeline dashboards."""

    monitor_dir = str(os.environ.get(LLM_MONITOR_DIR_ENV, "") or "").strip()
    if not monitor_dir:
        return
    try:
        root = Path(monitor_dir)
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "ts_utc": _utc_iso(),
            "pid": os.getpid(),
            "run_id": str(os.environ.get(LLM_MONITOR_RUN_ID_ENV, "") or "").strip(),
            **event,
        }
        path = root / f"llm_monitor_{os.getpid()}.jsonl"
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with _LLM_MONITOR_LOCK:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except Exception:
        return


def _monitor_response_fields(resp: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    try:
        choices = getattr(resp, "choices", None) or []
        if choices:
            finish_reason = getattr(choices[0], "finish_reason", None)
            if finish_reason is not None:
                fields["finish_reason"] = str(finish_reason)
    except Exception:
        pass
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            fields["usage_reported"] = True
            for attr in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(usage, attr, None)
                if value is not None:
                    fields[attr] = int(value)
            for detail, attr, dest in (("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
                                       ("prompt_tokens_details", "cached_tokens", "cached_input_tokens")):
                value = getattr(getattr(usage, detail, None), attr, None)
                if value is not None:
                    fields[dest] = int(value)
    except Exception:
        pass
    return fields


def _parse_error_kind(parse_error: str, payload: dict[str, Any]) -> str:
    detail = str(parse_error or "").lower()
    finish_reasons: list[str] = []
    response_full = payload.get("response_full")
    if isinstance(response_full, dict):
        choices = response_full.get("choices", [])
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    finish_reason = str(choice.get("finish_reason", "") or "").strip().lower()
                    if finish_reason:
                        finish_reasons.append(finish_reason)
    if "length" in finish_reasons:
        return "truncation"
    if "incomplete or malformed" in detail or "unterminated" in detail:
        return "truncation"
    if "cannot extract object" in detail or "invalid json payload" in detail or "json" in detail:
        return "json_parse"
    if "forbidden depth-1 entity names" in detail or "forbidden" in detail:
        return "validation"
    return "validation"


class OpenAIChatClient:
    """Thin resilient wrapper over openai.OpenAI chat.completions API."""

    def __init__(self, config: LLMConfig):
        if not config.api_key:
            raise ValueError("LLM api_key is empty.")
        if not config.base_url:
            raise ValueError("LLM base_url is empty.")
        if not config.model:
            raise ValueError("LLM model is empty.")

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise ImportError("openai package is required for llm mode. Install via requirements.txt.") from exc

        self.config = config
        self._http_client = httpx.Client(trust_env=not config.disable_env_proxy)
        # Disable SDK-level retries to keep timeout/retry behavior fully controlled by this wrapper.
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=0,
            http_client=self._http_client,
        )
        self._recorder: LLMReturnInfoRecorder | None = None
        self._cache_dir: Path | None = None
        if self.config.record_return_info and self.config.return_info_dir:
            self._recorder = _get_recorder(self.config.return_info_dir, self.config.run_tag)
        if self.config.cache_enabled and self.config.cache_dir:
            self._cache_dir = Path(self.config.cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def chat(
        self,
        messages: list[dict[str, Any]],
        task_type: str = "generic",
        call_context: dict[str, Any] | None = None,
        return_meta: bool = False,
        attempt_hook: Callable[[dict[str, Any]], None] | None = None,
        extra_body: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        include_response_full: bool = False,
    ) -> str | tuple[str, dict[str, Any]]:
        """Run one chat completion with retries and backoff."""
        attempts_total = max(1, int(self.config.max_retries))
        last_exc: Exception | None = None
        last_error: dict[str, Any] | None = None
        attempts: list[dict[str, Any]] = []
        call_t0 = time.perf_counter()
        cache_path = self._cache_path(
            task_type=task_type,
            messages=messages,
            extra_body=extra_body,
            tools=tools,
            tool_choice=tool_choice,
        )

        if cache_path is not None and cache_path.exists():
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_content = str(cached_payload.get("response_content", "") or "").strip()
            _emit_llm_monitor_event(
                {
                    "event": "cache_hit",
                    "task_type": task_type,
                    "model": self.config.model,
                    "base_url": self.config.base_url,
                    "attempt": 0,
                    "attempts_total": attempts_total,
                    "call_context": call_context or {},
                }
            )
            attempts.append(
                {
                    "attempt": 0,
                    "status": "cache_hit",
                    "elapsed_ms": 0,
                }
            )
            record_path = self._record_call(
                task_type=task_type,
                call_context=call_context or {},
                messages=messages,
                attempts=attempts,
                final_status="cache_hit",
                elapsed_ms=0,
                response_full=cached_payload.get("response_full"),
                response_content=cached_content,
                error=None,
                request_extra_body=extra_body,
                request_tools=tools,
                request_tool_choice=tool_choice,
            )
            if attempt_hook is not None:
                attempt_hook({"status": "cache_hit", "attempt": 0, "elapsed_ms": 0})
            if return_meta:
                meta = {"record_path": record_path, "cache_hit": True, "cache_path": str(cache_path)}
                if include_response_full:
                    meta["response_full"] = cached_payload.get("response_full")
                return cached_content, meta
            return cached_content

        for attempt in range(1, attempts_total + 1):
            attempt_id = _next_monitor_attempt_id()
            attempt_t0 = time.perf_counter()
            _emit_llm_monitor_event(
                {
                    "event": "attempt_start",
                    "attempt_id": attempt_id,
                    "task_type": task_type,
                    "model": self.config.model,
                    "base_url": self.config.base_url,
                    "attempt": attempt,
                    "attempts_total": attempts_total,
                    "call_context": call_context or {},
                }
            )
            try:
                api_kwargs: dict[str, Any] = {
                    "model": self.config.model,
                    "messages": messages,
                    "timeout": self.config.timeout,
                    "extra_body": extra_body or None,
                }
                if not self.config.omit_temperature:
                    api_kwargs["temperature"] = self.config.temperature
                if tools:
                    api_kwargs["tools"] = tools
                if tool_choice is not None:
                    api_kwargs["tool_choice"] = tool_choice
                resp = self._client.chat.completions.create(**api_kwargs)
                content = (resp.choices[0].message.content or "").strip()
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "ok",
                        "elapsed_ms": int((time.perf_counter() - attempt_t0) * 1000),
                    }
                )
                _emit_llm_monitor_event(
                    {
                        "event": "attempt_ok",
                        "attempt_id": attempt_id,
                        "task_type": task_type,
                        "model": self.config.model,
                        "base_url": self.config.base_url,
                        "attempt": attempt,
                        "attempts_total": attempts_total,
                        "elapsed_ms": attempts[-1]["elapsed_ms"],
                        "call_context": call_context or {},
                        **_monitor_response_fields(resp),
                    }
                )
                if attempt_hook is not None:
                    attempt_hook(
                        {
                            "attempt": attempt,
                            "attempts_total": attempts_total,
                            "status": "ok",
                            "elapsed_ms": attempts[-1]["elapsed_ms"],
                            **_monitor_response_fields(resp),
                        }
                    )
                self._write_cache(
                    cache_path=cache_path,
                    response_full=_safe_model_dump(resp),
                    response_content=content,
                )
                record_path = self._record_call(
                    task_type=task_type,
                    call_context=call_context or {},
                    messages=messages,
                    attempts=attempts,
                    final_status="ok",
                    elapsed_ms=int((time.perf_counter() - call_t0) * 1000),
                    response_full=_safe_model_dump(resp),
                    response_content=content,
                    error=None,
                    request_extra_body=extra_body,
                    request_tools=tools,
                    request_tool_choice=tool_choice,
                )
                if return_meta:
                    meta = {
                        "record_path": record_path,
                        "cache_hit": False,
                        "cache_path": str(cache_path) if cache_path is not None else "",
                    }
                    if include_response_full:
                        meta["response_full"] = _safe_model_dump(resp)
                    return content, meta
                return content
            except Exception as exc:  # pragma: no cover - network/runtime behavior
                last_exc = exc
                err = _classify_llm_error(exc)
                last_error = err
                elapsed_ms = int((time.perf_counter() - attempt_t0) * 1000)
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "error",
                        "elapsed_ms": elapsed_ms,
                        "error": {
                            "retryable": bool(err.get("retryable", False)),
                            "status_code": err.get("status_code"),
                            "error_type": str(err.get("error_type", "unknown")),
                            "detail": str(exc),
                        },
                    }
                )
                sleep_s = 0.0
                if err.get("retryable", False) and attempt < attempts_total:
                    sleep_s = _compute_retry_sleep_seconds(
                        exc=exc,
                        info=err,
                        attempt=attempt,
                        config=self.config,
                    )
                _emit_llm_monitor_event(
                    {
                        "event": "attempt_error",
                        "attempt_id": attempt_id,
                        "task_type": task_type,
                        "model": self.config.model,
                        "base_url": self.config.base_url,
                        "attempt": attempt,
                        "attempts_total": attempts_total,
                        "elapsed_ms": elapsed_ms,
                        "retryable": bool(err.get("retryable", False)),
                        "will_retry": bool(sleep_s > 0.0),
                        "next_retry_in_seconds": sleep_s,
                        "status_code": err.get("status_code"),
                        "error_type": str(err.get("error_type", "unknown")),
                        "detail": str(exc)[:500],
                        "call_context": call_context or {},
                    }
                )
                if attempt_hook is not None:
                    attempt_hook(
                        {
                            "attempt": attempt,
                            "attempts_total": attempts_total,
                            "status": "error",
                            "elapsed_ms": attempts[-1]["elapsed_ms"],
                            "error": attempts[-1]["error"],
                            "next_retry_in_seconds": sleep_s,
                        }
                    )
                if not err.get("retryable", False):
                    detail = _format_llm_error_message(exc, err, attempts=attempt, terminal=True)
                    self._record_call(
                        task_type=task_type,
                        call_context=call_context or {},
                        messages=messages,
                        attempts=attempts,
                        final_status="error",
                        elapsed_ms=int((time.perf_counter() - call_t0) * 1000),
                        response_full=None,
                        response_content="",
                        error=detail,
                        request_extra_body=extra_body,
                        request_tools=tools,
                        request_tool_choice=tool_choice,
                    )
                    raise RuntimeError(detail) from exc

                if attempt < attempts_total:
                    # Exponential backoff: backoff, 2*backoff, 4*backoff, ...
                    time.sleep(sleep_s)

        detail = _format_llm_error_message(last_exc, last_error, attempts=attempts_total, terminal=True)
        self._record_call(
            task_type=task_type,
            call_context=call_context or {},
            messages=messages,
            attempts=attempts,
            final_status="error",
            elapsed_ms=int((time.perf_counter() - call_t0) * 1000),
            response_full=None,
            response_content="",
            error=detail,
            request_extra_body=extra_body,
            request_tools=tools,
            request_tool_choice=tool_choice,
        )
        raise RuntimeError(detail)

    def _record_call(
        self,
        task_type: str,
        call_context: dict[str, Any],
        messages: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        final_status: str,
        elapsed_ms: int,
        response_full: Any,
        response_content: str,
        error: str | None,
        request_extra_body: dict[str, Any] | None = None,
        request_tools: list[dict[str, Any]] | None = None,
        request_tool_choice: Any | None = None,
    ) -> str | None:
        if self._recorder is None:
            return None
        payload = {
            "task_type": task_type,
            "call_context": call_context,
            "llm_config": {
                "base_url": self.config.base_url,
                "model": self.config.model,
                "temperature": self.config.temperature,
                "omit_temperature": self.config.omit_temperature,
                "timeout": self.config.timeout,
                "max_retries": self.config.max_retries,
                "backoff_seconds": self.config.backoff_seconds,
                "max_backoff_seconds": self.config.max_backoff_seconds,
                "rate_limit_max_wait_seconds": self.config.rate_limit_max_wait_seconds,
                "cache_enabled": self.config.cache_enabled,
                "cache_dir": self.config.cache_dir,
                "disable_env_proxy": self.config.disable_env_proxy,
            },
            "request_messages": messages,
            "request_extra_body": request_extra_body,
            "request_tools": request_tools,
            "request_tool_choice": request_tool_choice,
            "attempts": attempts,
            "final_status": final_status,
            "elapsed_ms": elapsed_ms,
            "response_full": response_full,
            "response_content": response_content,
            "error": error,
        }
        return self._recorder.record(payload)

    def annotate_record(
        self,
        record_path: str | None,
        parsed_object: dict[str, Any] | None = None,
        parse_error: str | None = None,
    ) -> None:
        """Attach parse outcome to an existing llm return record file."""
        if not record_path:
            return
        p = Path(record_path)
        if not p.exists():
            return
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            payload["parsed_object"] = parsed_object
            payload["parse_error"] = parse_error
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if parse_error:
                llm_config = payload.get("llm_config", {}) if isinstance(payload.get("llm_config"), dict) else {}
                parse_error_kind = _parse_error_kind(str(parse_error), payload)
                _emit_llm_monitor_event(
                    {
                        "event": "parse_error",
                        "parse_error_kind": parse_error_kind,
                        "is_truncation": parse_error_kind == "truncation",
                        "task_type": str(payload.get("task_type", "generic")),
                        "model": str(llm_config.get("model", self.config.model)),
                        "base_url": str(llm_config.get("base_url", self.config.base_url)),
                        "record_path": str(p),
                        "detail": str(parse_error)[:500],
                        "call_context": payload.get("call_context", {}) if isinstance(payload.get("call_context"), dict) else {},
                    }
                )
        except Exception:
            # Keep main flow robust; parse annotation failure should not break pipeline.
            return

    def _cache_path(
        self,
        task_type: str,
        messages: list[dict[str, Any]],
        extra_body: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> Path | None:
        """Build deterministic cache path for one request."""
        if self._cache_dir is None:
            return None
        key_payload = {
            "task_type": task_type,
            "base_url": self.config.base_url,
            "model": self.config.model,
            "temperature": self.config.temperature,
            "omit_temperature": self.config.omit_temperature,
            "messages": messages,
            "extra_body": extra_body or None,
            "tools": tools or None,
            "tool_choice": tool_choice,
        }
        digest = hashlib.sha256(
            json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _write_cache(
        self,
        cache_path: Path | None,
        response_full: Any,
        response_content: str,
    ) -> None:
        """Persist successful response to local prompt/result cache."""
        if cache_path is None:
            return
        payload = {
            "cached_at_utc": _utc_iso(),
            "model": self.config.model,
            "base_url": self.config.base_url,
            "response_full": response_full,
            "response_content": response_content,
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _classify_llm_error(exc: Exception) -> dict[str, Any]:
    """
    Classify LLM exceptions into retryable / non-retryable.

    Policy:
    - Retry: timeout, connection, 400, 408, 429, and 5xx.
    - Do not retry: most other 4xx request/auth/permission errors.
    """
    info: dict[str, Any] = {
        "retryable": False,
        "status_code": getattr(exc, "status_code", None),
        "error_type": type(exc).__name__,
    }

    if _is_transient_upstream_disabled_credential_error(exc, info["status_code"]):
        info["retryable"] = True
        info["error_type"] = "upstream_credential_disabled"
        return info

    try:
        import openai  # type: ignore
    except Exception:  # pragma: no cover
        openai = None

    if openai is not None:
        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            info["retryable"] = True
            info["error_type"] = "timeout_or_connection"
            return info
        if isinstance(exc, openai.RateLimitError):
            info["retryable"] = True
            info["status_code"] = info["status_code"] or 429
            info["error_type"] = "rate_limit"
            return info
        if isinstance(exc, openai.InternalServerError):
            info["retryable"] = True
            info["status_code"] = info["status_code"] or 500
            info["error_type"] = "server_error"
            return info
        if isinstance(exc, openai.APIStatusError):
            status = info["status_code"]
            if isinstance(status, int) and (status == 400 or status == 408 or status == 429 or status >= 500):
                info["retryable"] = True
            info["error_type"] = "api_status_error"
            return info

    # Fallback for lower-level transport timeouts.
    if isinstance(exc, TimeoutError):
        info["retryable"] = True
        info["error_type"] = "timeout"
        return info

    try:
        import httpx  # type: ignore
    except Exception:  # pragma: no cover
        httpx = None
    if httpx is not None and isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ),
    ):
        info["retryable"] = True
        info["error_type"] = "transport_error"
        return info

    return info


def _is_transient_upstream_disabled_credential_error(
    exc: Exception,
    status_code: Any,
) -> bool:
    """Retry only the known mixed-pool disabled-upstream 401 response."""
    if status_code != 401:
        return False
    detail = str(exc)
    # Routify currently emits both the generic ``禁用`` form and variants such
    # as ``主AK禁用``.  Keep the match narrow: an ordinary invalid caller key
    # must remain non-retryable, while a disabled credential inside the
    # gateway's mixed upstream pool is transient for the caller.
    return "API密钥状态异常" in detail and "禁用" in detail


def _compute_retry_sleep_seconds(
    exc: Exception,
    info: dict[str, Any] | None,
    attempt: int,
    config: LLMConfig,
) -> float:
    """Compute bounded retry sleep with special handling for rate limits."""
    info = info or {}
    status_code = info.get("status_code")
    error_type = str(info.get("error_type", "unknown"))
    base_backoff = max(0.0, float(config.backoff_seconds))
    max_backoff = max(0.0, float(config.max_backoff_seconds))
    rate_limit_cap = max(0.0, float(config.rate_limit_max_wait_seconds))

    if status_code == 429 or error_type == "rate_limit" or _message_mentions_rate_limit(str(exc)):
        retry_after_s = _extract_retry_after_seconds(exc)
        if retry_after_s is None:
            retry_after_s = _extract_limit_reset_wait_seconds(exc)
        if retry_after_s is not None:
            return _jitter_seconds(min(rate_limit_cap, max(0.0, retry_after_s)), 0.95, 1.10)

        # Use a gentler capped curve for repeated 429s.
        base_sleep_s = min(rate_limit_cap, base_backoff * (1.5 ** max(0, attempt - 1)))
        return _jitter_seconds(base_sleep_s, 0.85, 1.15)

    base_sleep_s = min(max_backoff, base_backoff * (2 ** max(0, attempt - 1)))
    return _jitter_seconds(base_sleep_s, 0.75, 1.25)


def _jitter_seconds(seconds: float, lo: float, hi: float) -> float:
    """Apply bounded multiplicative jitter to a positive sleep interval."""
    seconds = max(0.0, float(seconds))
    if seconds <= 0:
        return 0.0
    return random.uniform(seconds * lo, seconds * hi)


def _message_mentions_rate_limit(detail: str) -> bool:
    text = (detail or "").lower()
    return "rate limit" in text or "rate_limit" in text


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    """Read retry-after semantics from headers or fallback message text."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        header_value = headers.get("retry-after") or headers.get("Retry-After")
        seconds = _parse_retry_after_value(str(header_value or "").strip())
        if seconds is not None:
            return seconds

    detail = str(exc)
    match = re.search(r"retry[- ]after[:= ]+([0-9]+(?:\.[0-9]+)?)", detail, flags=re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _extract_limit_reset_wait_seconds(exc: Exception) -> float | None:
    """Parse absolute reset timestamps exposed by some upstream rate-limit errors."""
    detail = str(exc)
    match = re.search(
        r"Limit resets at:\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC)",
        detail,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        reset_dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (reset_dt - datetime.now(timezone.utc)).total_seconds())


def _parse_retry_after_value(raw_value: str) -> float | None:
    """Parse HTTP Retry-After as either seconds or absolute HTTP-date."""
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    for fmt in ("%a, %d %b %Y %H:%M:%S GMT", "%A, %d-%b-%y %H:%M:%S GMT"):
        try:
            retry_dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return max(0.0, (retry_dt - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            continue
    return None


def _format_llm_error_message(
    exc: Exception | None,
    info: dict[str, Any] | None,
    attempts: int,
    terminal: bool,
) -> str:
    """Create a compact error message with classification details."""
    detail = str(exc) if exc is not None else "unknown_error"
    info = info or {}
    retryable = bool(info.get("retryable", False))
    status_code = info.get("status_code")
    error_type = str(info.get("error_type", "unknown"))

    prefix = "LLM call failed"
    if terminal:
        prefix = "LLM call terminal failure"
    return (
        f"{prefix} after {attempts} attempts: "
        f"retryable={retryable} status_code={status_code} error_type={error_type} detail={detail}"
    )


def parse_first_json_object(text: str) -> dict[str, Any]:
    """
    Extract and parse the first valid JSON object from model output.

    Tolerates extra wrapper text, markdown fences, and think tags.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("LLM output is empty.")

    stripped = _clean_text_before_json_parse(text)
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as exc:
        if stripped.lstrip().startswith("{"):
            preview_start = max(0, exc.pos - 120)
            preview_end = min(len(stripped), exc.pos + 120)
            preview = stripped[preview_start:preview_end].replace("\n", "\\n")
            raise ValueError(
                "Invalid JSON payload from LLM: top-level object is incomplete or malformed; "
                f"error={exc.msg} at pos={exc.pos}; near={preview}"
            ) from exc

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(stripped):
        if ch != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    preview = stripped[:280].replace("\n", "\\n")
    raise ValueError(f"Invalid JSON payload from LLM: cannot extract object; preview={preview}")


def _clean_text_before_json_parse(text: str) -> str:
    """Remove frequent wrapper artifacts before JSON extraction."""
    cleaned = text.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


def _safe_model_dump(obj: Any) -> Any:
    """Best-effort conversion of SDK response object to plain JSON-serializable structure."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    return str(obj)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm_opt_str(value: Any) -> str:
    """Normalize optional text values where None/'None' should be treated as empty."""
    if value is None:
        return ""
    txt = str(value).strip()
    if txt.lower() in {"none", "null"}:
        return ""
    return txt


def _coerce_bool(value: Any, default: bool) -> bool:
    """Parse common string/int/bool representations into a stable boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def invalidate_cache_path(cache_path: str | None) -> None:
    """Delete one cached LLM response after parse/validation failure."""
    path_text = str(cache_path or "").strip()
    if not path_text:
        return
    try:
        path = Path(path_text)
        if path.exists():
            path.unlink()
    except Exception:
        return
