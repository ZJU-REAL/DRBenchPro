"""Bounded OpenAI-compatible client with ordered route fallback."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol

import requests

from .schemas import ChatResponse, TokenUsage, ToolCall


class ModelClientError(RuntimeError):
    """Model failure with a secret-free ledger of real API attempts."""

    def __init__(
        self,
        message: str,
        *,
        attempts: list[dict[str, Any]] | None = None,
        usage_by_route: list[dict[str, Any]] | None = None,
        purpose: str = "",
        route_failures: list[dict[str, Any]] | None = None,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.attempts = [dict(item) for item in (attempts or [])]
        self.usage_by_route = [dict(item) for item in (usage_by_route or [])]
        self.purpose = str(purpose or "")
        self.route_failures = [dict(item) for item in (route_failures or [])]
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


class ChatModel(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        purpose: str = "research",
        max_tokens: int | None = None,
    ) -> ChatResponse: ...


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]

_FORBIDDEN_REMOTE_SEARCH_KEYS = {
    "enable_search",
    "plugins",
    "search_options",
    "tools",
    "tool_choice",
    "web_search",
    "web_search_options",
}
_LEADING_THINK_RE = re.compile(
    r"^\s*<think\b[^>]*>.*?</think>\s*",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_RESEARCH_TOOL_NAMES = {"search", "open"}
_REMOTE_SEARCH_RESPONSE_KEYS = {
    "web_search",
    "web_search_call",
    "web_search_calls",
    "web_search_request",
    "web_search_requests",
}


@dataclass
class ModelConfig:
    base_url: str
    model: str
    api_key: str
    source: str = "openai_compatible"
    provider: str = "openai_compatible"
    timeout_seconds: float = 600.0
    connect_timeout_seconds: float = 30.0
    # One logical model call may make the initial request plus this many
    # additional attempts. The release default is deliberately generous to
    # tolerate transient provider errors under concurrent requests.
    max_retries: int = 10
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    backoff_jitter_ratio: float = 0.0
    max_tokens: int = 8192
    temperature: float = 0.2
    omit_temperature: bool = False
    disable_env_proxy: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ModelConfig":
        if not isinstance(value, dict):
            raise TypeError("Model route must be a mapping.")
        nested = value.get("model")
        merged = {**(nested if isinstance(nested, dict) else {}), **value}
        api_key = _resolve_api_key(merged)
        base_url = str(merged.get("base_url", "") or "").strip()
        model = str(merged.get("model_id") or merged.get("model") or "").strip()
        if isinstance(nested, dict):
            model = str(merged.get("model_id") or nested.get("model") or "").strip()
        if not base_url:
            raise ValueError("Model route base_url is empty.")
        if not model:
            raise ValueError("Model route model/model_id is empty.")
        if not api_key:
            raise ValueError(
                "Model route api_key is empty; provide api_key or api_key_env."
            )
        request_overrides = dict(merged.get("request_overrides") or {})
        extra_body = dict(merged.get("extra_body") or {})
        forbidden = _FORBIDDEN_REMOTE_SEARCH_KEYS.intersection(
            {*request_overrides, *extra_body}
        )
        if forbidden:
            raise ValueError(
                "Model route may not configure remote/provider search or override "
                f"the local tool contract: {sorted(forbidden)}."
            )
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            source=str(merged.get("source") or merged.get("name") or "openai_compatible"),
            provider=str(merged.get("provider") or "openai_compatible"),
            timeout_seconds=max(1.0, float(merged.get("timeout_seconds", merged.get("timeout", 600.0)))),
            connect_timeout_seconds=max(1.0, float(merged.get("connect_timeout_seconds", 30.0))),
            max_retries=min(10, max(0, int(merged.get("max_retries", 10)))),
            backoff_seconds=max(0.0, float(merged.get("backoff_seconds", 1.0))),
            max_backoff_seconds=max(0.0, float(merged.get("max_backoff_seconds", 8.0))),
            backoff_jitter_ratio=min(
                1.0,
                max(0.0, float(merged.get("backoff_jitter_ratio", 0.0))),
            ),
            max_tokens=max(128, int(merged.get("max_tokens", merged.get("max_completion_tokens", 8192)))),
            temperature=float(merged.get("temperature", 0.2)),
            omit_temperature=bool(merged.get("omit_temperature", False)),
            disable_env_proxy=bool(merged.get("disable_env_proxy", True)),
            headers={str(k): str(v) for k, v in (merged.get("headers") or {}).items()},
            extra_body={
                **request_overrides,
                **extra_body,
            },
        )


class OpenAICompatibleModelClient:
    """Chat Completions client that keeps retries and usage accounting explicit."""

    RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        config: ModelConfig | dict[str, Any],
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ):
        self.config = (
            config if isinstance(config, ModelConfig) else ModelConfig.from_mapping(config)
        )
        self._transport = transport
        self._sleep = sleep
        self._random_value = random_value
        self._openrouter_tool_choice_mode = "specific"

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        purpose: str = "research",
        max_tokens: int | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            # Compatibility repair messages must remain local to this API call.
            # Mutating the agent-owned list would leak provider-specific recovery
            # instructions into later research rounds.
            "messages": [dict(message) for message in messages],
            "max_tokens": max(128, int(max_tokens or self.config.max_tokens)),
        }
        if not self.config.omit_temperature:
            payload["temperature"] = self.config.temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
            _apply_openrouter_tool_choice_mode(
                payload,
                mode=self._openrouter_tool_choice_mode,
            )
        payload.update(self.config.extra_body)
        if self.config.provider.strip().casefold() == "openrouter":
            usage_options = payload.get("usage")
            if not isinstance(usage_options, dict):
                usage_options = {}
            payload["usage"] = {**usage_options, "include": True}
        request_audit = audit_local_tool_only_exchange(payload)

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        for key, value in self.config.headers.items():
            if key.lower() != "authorization":
                headers[key] = value

        url = _chat_completions_url(self.config.base_url)
        started = time.perf_counter()
        retries_used = 0
        thinking_tool_choice_repairs = 0
        attempt = 0
        last_error: Exception | None = None
        data: dict[str, Any] | None = None
        successful_accounting: dict[str, Any] = {}
        attempt_records: list[dict[str, Any]] = []
        local_tool_protocol_repairs = 0
        transient_provider_repairs = 0
        while True:
            attempt += 1
            attempt_started = time.perf_counter()
            attempt_accounting: dict[str, Any] = {}
            attempt_usage: TokenUsage | None = None
            response_received = False
            try:
                data = self._post(url, headers, payload)
                response_received = True
                attempt_accounting = _provider_accounting(self.config, data)
                attempt_usage = _normalize_usage(data.get("usage"))
                attempt_usage.api_calls = 1
                response_audit = audit_local_tool_only_exchange(payload, data)
                data["local_tool_only_audit"] = {
                    **request_audit,
                    **response_audit,
                }
                _validate_response_envelope(data)
                if (
                    self.config.provider.strip().casefold() == "openrouter"
                    and not attempt_accounting
                ):
                    attempt_accounting = _provider_accounting(
                        self.config,
                        data,
                        require_cost_status=True,
                    )
                successful_accounting = attempt_accounting
                attempt_records.append(
                    {
                        "attempt_index": attempt,
                        "status": "success",
                        "latency_seconds": round(
                            time.perf_counter() - attempt_started, 6
                        ),
                        "http_status": 200,
                        "error_type": "",
                        "error_message": "",
                        "response_usage": attempt_usage.to_dict(),
                        **successful_accounting,
                    }
                )
                break
            except Exception as exc:
                last_error = exc
                has_retry = retries_used < self.config.max_retries
                # This exact parameter repair is separate from network retries.
                # A no-network-retry probe may therefore still adapt once.
                thinking_repair = (
                    thinking_tool_choice_repairs == 0
                    and _is_thinking_tool_choice_error(exc, payload)
                )
                compatibility_fallback = (
                    _openrouter_tool_choice_compatibility_fallback(
                        self.config,
                        exc,
                        payload,
                    )
                    if has_retry or thinking_repair
                    else ""
                )
                local_tool_protocol_retry = (
                    has_retry
                    and
                    local_tool_protocol_repairs < self.config.max_retries
                    and _is_unrequested_local_tool_response(exc)
                )
                transient_provider_retry = (
                    has_retry
                    and
                    transient_provider_repairs < self.config.max_retries
                    and _is_transient_provider_error(self.config, exc)
                )
                compatibility_retry = bool(compatibility_fallback)
                retryable = (
                    compatibility_retry
                    or local_tool_protocol_retry
                    or transient_provider_retry
                    or (has_retry and _is_retryable_exception(exc))
                )
                if compatibility_retry:
                    if thinking_repair:
                        thinking_tool_choice_repairs += 1
                    if compatibility_fallback == "omit_tools_for_synthesis":
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                    elif compatibility_fallback == "generic_required_tool_choice":
                        self._openrouter_tool_choice_mode = "generic_required"
                        _apply_openrouter_tool_choice_mode(
                            payload,
                            mode=self._openrouter_tool_choice_mode,
                        )
                    else:
                        self._openrouter_tool_choice_mode = "auto_single_tool"
                        _apply_openrouter_tool_choice_mode(
                            payload,
                            mode=self._openrouter_tool_choice_mode,
                        )
                    retry_delay = 0.0
                    retry_delay_source = "openrouter_tool_choice_compatibility"
                elif local_tool_protocol_retry:
                    local_tool_protocol_repairs += 1
                    _append_local_tool_protocol_repair(
                        payload,
                        repair_index=local_tool_protocol_repairs,
                        max_repairs=self.config.max_retries,
                    )
                    retry_delay = 0.0
                    retry_delay_source = "local_tool_protocol_repair"
                elif transient_provider_retry:
                    transient_provider_repairs += 1
                    retry_delay, retry_delay_source = _retry_delay(
                        self.config,
                        exc,
                        attempt=retries_used + 1,
                        has_next_attempt=has_retry,
                        random_value=self._random_value,
                    )
                    retry_delay_source = (
                        f"transient_provider_http_{getattr(exc, 'http_status', 'unknown')}:"
                        f"{retry_delay_source}"
                    )
                else:
                    retry_delay, retry_delay_source = _retry_delay(
                        self.config,
                        exc,
                        attempt=retries_used + 1,
                        has_next_attempt=has_retry and retryable,
                        random_value=self._random_value,
                    )
                attempt_records.append(
                    {
                        "attempt_index": attempt,
                        "status": "error",
                        "latency_seconds": round(
                            time.perf_counter() - attempt_started, 6
                        ),
                        "http_status": (
                            200
                            if response_received
                            else getattr(exc, "http_status", None)
                        ),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "retry_delay_seconds": retry_delay,
                        "retry_delay_source": retry_delay_source,
                        "compatibility_fallback": compatibility_fallback,
                        "local_tool_protocol_repair": local_tool_protocol_retry,
                        "transient_provider_repair": transient_provider_retry,
                        "transient_provider_400_repair": (
                            transient_provider_retry
                            and getattr(exc, "http_status", None) == 400
                        ),
                        "transient_provider_auth_repair": (
                            transient_provider_retry
                            and getattr(exc, "http_status", None) == 401
                        ),
                        **(
                            {"response_usage": attempt_usage.to_dict()}
                            if attempt_usage is not None
                            else {}
                        ),
                        **attempt_accounting,
                    }
                )
                if not retryable:
                    raise ModelClientError(
                        f"{self.config.source} model call failed after {attempt} attempt(s): {exc}",
                        attempts=_tag_attempts(
                            attempt_records,
                            route=_route_identity(self.config),
                        ),
                        usage_by_route=[
                            _route_usage(
                                self.config,
                                _aggregate_attempt_usage(attempt_records),
                                accounting=_aggregate_attempt_accounting(
                                    attempt_records
                                ),
                            )
                        ],
                        purpose=purpose,
                        route_failures=[
                            {
                                **_route_identity(self.config),
                                "attempt_count": len(attempt_records),
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                            }
                        ],
                        http_status=(
                            200
                            if response_received
                            else getattr(exc, "http_status", None)
                        ),
                    ) from exc
                if not (compatibility_retry and thinking_repair):
                    retries_used += 1
                if retry_delay:
                    self._sleep(retry_delay)
        if data is None:  # pragma: no cover - guarded by loop above
            raise ModelClientError(f"Model call failed: {last_error}")

        elapsed = time.perf_counter() - started
        choice = data["choices"][0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise ModelClientError("Chat completion first choice has no message object.")
        content = _strip_leading_think(_content_text(message.get("content")))
        parsed_tool_calls = _parse_tool_calls(message.get("tool_calls"))
        usage = _aggregate_attempt_usage(attempt_records)
        route_identity = _route_identity(self.config)
        return ChatResponse(
            content=content,
            tool_calls=parsed_tool_calls,
            usage=usage,
            raw_response=data,
            request_payload={
                **payload,
                "purpose": purpose,
                "url": url,
                "route_source": self.config.source,
            },
            finish_reason=str(choice.get("finish_reason", "") or ""),
            model=str(data.get("model") or self.config.model),
            provider=str(data.get("provider") or ""),
            latency_seconds=elapsed,
            route={
                **route_identity,
                "fallback_used": False,
                "attempt_count": len(attempt_records),
                "tool_choice_mode": self._openrouter_tool_choice_mode,
                "thinking_tool_choice_repairs": thinking_tool_choice_repairs,
                "route_attempts": _tag_attempts(
                    attempt_records,
                    route=route_identity,
                ),
                "usage_by_route": [
                    _route_usage(
                        self.config,
                        usage,
                        accounting=_aggregate_attempt_accounting(attempt_records),
                    )
                ],
                "mixed_route": False,
            },
        )

    def _post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self._transport is not None:
            result = self._transport(url, headers, payload, self.config.timeout_seconds)
            if not isinstance(result, dict):
                raise ModelClientError("Injected model transport returned non-object JSON.")
            return result

        session = requests.Session()
        session.trust_env = not self.config.disable_env_proxy
        try:
            response = session.post(
                url,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=(self.config.connect_timeout_seconds, self.config.timeout_seconds),
            )
        except requests.RequestException as exc:
            raise _RetryableModelError(str(exc)) from exc
        finally:
            session.close()
        if response.status_code in self.RETRYABLE_STATUS_CODES:
            raise _RetryableModelError(
                f"HTTP {response.status_code}: {response.text[:1000]}",
                http_status=response.status_code,
                retry_after_seconds=_response_retry_after_seconds(response),
            )
        if response.status_code >= 400:
            raise ModelClientError(
                f"HTTP {response.status_code}: {response.text[:2000]}",
                http_status=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise _RetryableModelError("Provider returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise _RetryableModelError("Provider returned non-object JSON.")
        return data


class RoutedModelClient:
    """Use ordered routes and stick to the first route that succeeds."""

    def __init__(
        self,
        routes: list[ChatModel],
        *,
        fallback_policy: str = "all_model_errors",
    ):
        if not routes:
            raise ValueError("RoutedModelClient needs at least one route.")
        if fallback_policy not in {"provider_errors_only", "all_model_errors"}:
            raise ValueError(f"Unsupported route fallback policy: {fallback_policy!r}")
        self.routes = list(routes)
        self.fallback_policy = fallback_policy
        self._active_index = 0
        self._lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        purpose: str = "research",
        max_tokens: int | None = None,
    ) -> ChatResponse:
        with self._lock:
            start_index = self._active_index
        errors: list[str] = []
        failed_attempts: list[dict[str, Any]] = []
        failed_usage_by_route: list[dict[str, Any]] = []
        route_failures: list[dict[str, Any]] = []
        for route_index in range(start_index, len(self.routes)):
            route = self.routes[route_index]
            try:
                response = route.chat(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    purpose=purpose,
                    max_tokens=max_tokens,
                )
            except ModelClientError as exc:
                if (
                    self.fallback_policy == "provider_errors_only"
                    and not _is_provider_route_error(exc)
                ):
                    raise
                errors.append(f"route[{route_index}] {exc}")
                failed_attempts.extend(
                    _with_route_index(exc.attempts, route_index=route_index)
                )
                failed_usage_by_route.extend(
                    _with_route_index(exc.usage_by_route, route_index=route_index)
                )
                if exc.route_failures:
                    route_failures.extend(
                        _with_route_index(exc.route_failures, route_index=route_index)
                    )
                else:
                    route_failures.append(
                        {
                            "route_index": route_index,
                            "attempt_count": len(exc.attempts),
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        }
                    )
                continue
            with self._lock:
                if route_index > self._active_index:
                    self._active_index = route_index
            successful_attempts = _with_route_index(
                response.route.get("route_attempts") or [],
                route_index=route_index,
            )
            successful_usage = _with_route_index(
                response.route.get("usage_by_route") or [],
                route_index=route_index,
            )
            usage_by_route = _merge_route_usage(
                [*failed_usage_by_route, *successful_usage]
            )
            failed_api_calls = sum(
                int(item.get("api_calls", 0) or 0)
                for item in failed_usage_by_route
            )
            response.usage.api_calls += failed_api_calls
            response.route = {
                **response.route,
                "route_index": route_index,
                "fallback_used": route_index > 0,
                "fallback_errors": list(errors),
                "route_failures": list(route_failures),
                "route_attempts": [*failed_attempts, *successful_attempts],
                "attempt_count": response.usage.api_calls,
                "successful_route_attempt_count": len(successful_attempts),
                "usage_by_route": usage_by_route,
                "mixed_route": _mixed_route(usage_by_route),
            }
            return response
        usage_by_route = _merge_route_usage(failed_usage_by_route)
        raise ModelClientError(
            "All ordered model routes failed: " + " | ".join(errors),
            attempts=failed_attempts,
            usage_by_route=usage_by_route,
            purpose=purpose,
            route_failures=route_failures,
        )


class UnavailableModelClient:
    """Ordered-route placeholder for a credential missing at service startup."""

    def __init__(self, *, source: str, reason: str):
        self.source = source
        self.reason = reason

    def chat(self, *args: Any, **kwargs: Any) -> ChatResponse:
        message = f"{self.source} route unavailable: {self.reason}"
        raise ModelClientError(
            message,
            purpose=str(kwargs.get("purpose", "") or ""),
            route_failures=[
                {
                    "source": self.source,
                    "attempt_count": 0,
                    "error_type": "RouteUnavailable",
                    "error_message": message,
                }
            ],
        )


class _RetryableModelError(ModelClientError):
    pass


def _is_provider_route_error(error: ModelClientError) -> bool:
    """Return whether an error represents an unavailable provider route."""

    return bool(
        error.attempts
        or error.route_failures
        or error.usage_by_route
        or error.http_status is not None
    )


def _resolve_api_key(value: dict[str, Any]) -> str:
    api_key = str(value.get("api_key", "") or "").strip()
    env_name = str(value.get("api_key_env", "") or "").strip()
    if not api_key and env_name:
        api_key = str(os.getenv(env_name, "") or "").strip()
    if api_key.startswith("${") and api_key.endswith("}"):
        api_key = str(os.getenv(api_key[2:-1], "") or "").strip()
    elif api_key.startswith("$") and api_key[1:].replace("_", "").isalnum():
        api_key = str(os.getenv(api_key[1:], "") or "").strip()
    return api_key


def _chat_completions_url(base_url: str) -> str:
    value = str(base_url or "").rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def _validate_response_envelope(data: dict[str, Any]) -> None:
    error = data.get("error")
    if error:
        raise _RetryableModelError(f"Provider error payload: {str(error)[:1500]}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _RetryableModelError("Chat completion response has no choices.")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _RetryableModelError("Chat completion first choice is not an object.")
    if not isinstance(choice.get("message"), dict):
        raise _RetryableModelError(
            "Chat completion first choice has no message object."
        )


def audit_local_tool_only_exchange(
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless an exchange uses only local function tools."""

    forbidden_request_keys = _FORBIDDEN_REMOTE_SEARCH_KEYS.intersection(
        str(key).strip().casefold() for key in request_payload
    ) - {"tools", "tool_choice"}
    if forbidden_request_keys:
        raise ModelClientError(
            "Model request contains provider/remote search fields: "
            f"{sorted(forbidden_request_keys)}."
        )

    request_tool_names: list[str] = []
    raw_tools = request_payload.get("tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, list):
            raise ModelClientError("Model request tools must be a list.")
        for index, tool in enumerate(raw_tools, start=1):
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ModelClientError(
                    f"Model request tool[{index}] is not a function tool."
                )
            function = tool.get("function")
            name = (
                str(function.get("name") or "").strip()
                if isinstance(function, dict)
                else ""
            )
            if name not in _LOCAL_RESEARCH_TOOL_NAMES:
                raise ModelClientError(
                    f"Model request tool[{index}] is not an allowed local tool: {name!r}."
                )
            request_tool_names.append(name)

    response_tool_names: list[str] = []
    if response_payload is not None:
        remote_markers = _find_remote_search_markers(response_payload)
        if remote_markers:
            raise ModelClientError(
                "Provider response indicates native/server Web Search: "
                f"{sorted(remote_markers)}."
            )
        choices = response_payload.get("choices")
        if isinstance(choices, list):
            for choice_index, choice in enumerate(choices, start=1):
                message = choice.get("message") if isinstance(choice, dict) else None
                tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
                if tool_calls is None:
                    continue
                if not isinstance(tool_calls, list):
                    raise ModelClientError(
                        f"Provider response choices[{choice_index}].message.tool_calls "
                        "must be a list."
                    )
                for call_index, call in enumerate(tool_calls, start=1):
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise ModelClientError(
                            "Provider returned a non-function tool call at "
                            f"choices[{choice_index}].tool_calls[{call_index}]."
                        )
                    function = call.get("function")
                    name = (
                        str(function.get("name") or "").strip()
                        if isinstance(function, dict)
                        else ""
                    )
                    if name not in _LOCAL_RESEARCH_TOOL_NAMES:
                        raise ModelClientError(
                            f"Provider returned disallowed tool call {name!r}."
                        )
                    if name not in request_tool_names:
                        raise ModelClientError(
                            f"Provider returned unrequested tool call {name!r}."
                        )
                    response_tool_names.append(name)

    return {
        "verified": True,
        "request_tool_names": request_tool_names,
        "response_tool_names": response_tool_names,
        "provider_web_search_requests": 0,
    }


def _find_remote_search_markers(value: Any, *, path: str = "response") -> set[str]:
    markers: set[str] = set()
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().casefold()
            child_path = f"{path}.{key}"
            if key in _REMOTE_SEARCH_RESPONSE_KEYS and _nonempty_marker(child):
                markers.add(child_path)
            if key in {"annotations", "citations"} and _nonempty_marker(child):
                markers.add(child_path)
            markers.update(_find_remote_search_markers(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            markers.update(_find_remote_search_markers(child, path=f"{path}[{index}]"))
    return markers


def _nonempty_marker(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _is_retryable_exception(exc: Exception) -> bool:
    return isinstance(exc, _RetryableModelError)


def _specific_tool_choice_name(value: Any) -> str:
    if not isinstance(value, dict) or value.get("type") != "function":
        return ""
    function = value.get("function")
    if not isinstance(function, dict):
        return ""
    return str(function.get("name") or "").strip()


def _apply_generic_required_tool_choice(payload: dict[str, Any]) -> bool:
    tool_name = _specific_tool_choice_name(payload.get("tool_choice"))
    tools = payload.get("tools")
    if not tool_name or not isinstance(tools, list):
        return False
    matching_tools = [
        tool
        for tool in tools
        if isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and str(tool["function"].get("name") or "").strip() == tool_name
    ]
    if not matching_tools:
        return False
    payload["tools"] = matching_tools
    payload["tool_choice"] = "required"
    return True


def _apply_openrouter_tool_choice_mode(
    payload: dict[str, Any],
    *,
    mode: str,
) -> bool:
    if mode == "specific":
        return False
    if mode == "generic_required":
        return _apply_generic_required_tool_choice(payload)
    if mode != "auto_single_tool":
        return False
    tools = payload.get("tools")
    if _specific_tool_choice_name(payload.get("tool_choice")):
        if not _apply_generic_required_tool_choice(payload):
            return False
        tools = payload.get("tools")
    if not isinstance(tools, list) or len(tools) != 1:
        return False
    payload["tool_choice"] = "auto"
    return True


def _is_thinking_tool_choice_error(exc: Exception, payload: dict[str, Any]) -> bool:
    """Recognize only the explicit thinking/forced-tool parameter rejection."""
    message = str(exc).casefold()
    tools = payload.get("tools")
    if (getattr(exc, "http_status", None) != 400
            or "tool_choice parameter does not support being set to required or object" not in message
            or "thinking mode" not in message or not isinstance(tools, list)):
        return False
    name = _specific_tool_choice_name(payload.get("tool_choice"))
    if not name and payload.get("tool_choice") == "required" and len(tools) == 1:
        name = tools[0].get("function", {}).get("name", "")
    return name in _LOCAL_RESEARCH_TOOL_NAMES and any(
        tool.get("function", {}).get("name") == name for tool in tools
    )


def _openrouter_tool_choice_compatibility_fallback(
    config: ModelConfig,
    exc: Exception,
    payload: dict[str, Any],
) -> str:
    # The explicit error is sufficient even with a generic compatible config.
    if _is_thinking_tool_choice_error(exc, payload):
        return "auto_single_tool_choice"
    if config.provider.strip().casefold() not in {"openrouter", "litellm"}:
        return ""
    http_status = getattr(exc, "http_status", None)
    message = str(exc).casefold()
    tools = payload.get("tools")
    tool_choice = payload.get("tool_choice")
    if http_status == 404 and "no endpoints found" in message and "tool_choice" in message:
        if _specific_tool_choice_name(tool_choice):
            return "generic_required_tool_choice"
        if tool_choice == "required" and isinstance(tools, list) and len(tools) == 1:
            return "auto_single_tool_choice"
        if tool_choice == "none" and isinstance(tools, list) and tools:
            return "omit_tools_for_synthesis"
    if (
        http_status == 400
        and "tools` must not be an empty array" in message
        and tool_choice == "none"
        and isinstance(tools, list)
        and tools
    ):
        return "omit_tools_for_synthesis"
    return ""


def _is_unrequested_local_tool_response(exc: Exception) -> bool:
    """Return true only for a safe ordinary-local-tool protocol mismatch."""

    message = str(exc).casefold()
    return any(
        f"provider returned unrequested tool call '{name}'" in message
        for name in sorted(_LOCAL_RESEARCH_TOOL_NAMES)
    )


def _append_local_tool_protocol_repair(
    payload: dict[str, Any],
    *,
    repair_index: int,
    max_repairs: int,
) -> None:
    """Retry a rejected response without broadening the declared tool surface."""

    tools = payload.get("tools")
    declared = []
    if isinstance(tools, list):
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            name = (
                str(function.get("name") or "").strip()
                if isinstance(function, dict)
                else ""
            )
            if name in _LOCAL_RESEARCH_TOOL_NAMES:
                declared.append(name)
    allowed = ", ".join(sorted(set(declared))) or "no tools"
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    messages.append(
        {
            "role": "user",
            "content": (
                f"Local tool protocol repair {repair_index}/{max_repairs}: the "
                "previous response selected a function that was not declared for "
                f"this request. Retry now and call only: {allowed}. The declared "
                "function name and its argument schema are mandatory. Do not name, "
                "call, or repeat any other function."
            ),
        }
    )


def _is_transient_provider_400(
    config: ModelConfig,
    exc: Exception,
) -> bool:
    """Recognize transient provider failures that were incorrectly surfaced as 400.

    OpenRouter and OpenAI-compatible LiteLLM gateways occasionally wrap an
    upstream overload, timeout, or rate limit in HTTP 400.  Match only explicit
    transient markers; arbitrary 400s (bad parameters, unsupported tools,
    authentication, and other permanent request errors) remain fail-fast.
    """

    if getattr(exc, "http_status", None) != 400:
        return False
    message = str(exc).casefold()
    if "venice" in message and any(
        marker in message
        for marker in (
            "invalid request parameters",
            "expecting value:",
        )
    ):
        return True
    transient_markers = (
        "rate limit",
        "rate_limit",
        "ratelimit",
        "too many requests",
        "retry after",
        "retry-after",
        "temporarily unavailable",
        "temporary unavailable",
        "no healthy deployment",
        "no healthy endpoint",
        "overloaded",
        "over capacity",
        "capacity exceeded",
        "upstream timeout",
        "upstream timed out",
        "gateway timeout",
        "request timeout",
        "request timed out",
        "service unavailable",
    )
    return any(marker in message for marker in transient_markers)


def _is_transient_provider_error(config: ModelConfig, exc: Exception) -> bool:
    """Recognize narrow transient wrappers without retrying ordinary auth errors."""

    if _is_transient_provider_400(config, exc):
        return True
    if getattr(exc, "http_status", None) != 401:
        return False
    if config.provider.strip().casefold() != "litellm":
        return False
    message = str(exc).casefold()
    return (
        "api密钥状态异常：禁用" in message
        or "api密钥状态异常: 禁用" in message
        or "api key status disabled" in message
    )


def _retry_delay(
    config: ModelConfig,
    exc: Exception,
    *,
    attempt: int,
    has_next_attempt: bool,
    random_value: Callable[[], float],
) -> tuple[float, str]:
    if not has_next_attempt:
        return 0.0, ""
    retry_after = getattr(exc, "retry_after_seconds", None)
    if retry_after is not None:
        base_delay = min(config.max_backoff_seconds, max(0.0, float(retry_after)))
        source = "retry_after"
    else:
        base_delay = min(
            config.max_backoff_seconds,
            config.backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        source = "exponential"
    if not base_delay or not config.backoff_jitter_ratio:
        return base_delay, source
    # Positive jitter honors Retry-After as a minimum and prevents concurrent
    # workers from retrying in lockstep after a shared overload response.
    jitter = base_delay * config.backoff_jitter_ratio * min(
        1.0, max(0.0, float(random_value()))
    )
    return min(config.max_backoff_seconds, base_delay + jitter), source


def _response_retry_after_seconds(response: requests.Response) -> float | None:
    value = str(response.headers.get("Retry-After", "") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part for part in parts if part).strip()


def _strip_leading_think(value: str) -> str:
    return _LEADING_THINK_RE.sub("", str(value or ""), count=1).strip()


def _parse_tool_calls(value: Any) -> list[ToolCall]:
    if not isinstance(value, list):
        return []
    calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name", "") or "").strip()
        arguments_value = function.get("arguments", "{}")
        raw = (
            arguments_value
            if isinstance(arguments_value, str)
            else json.dumps(arguments_value, ensure_ascii=False)
        )
        parsed: dict[str, Any] = {}
        parse_error = ""
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("tool arguments must decode to an object")
            parsed = decoded
        except (ValueError, json.JSONDecodeError) as exc:
            parse_error = str(exc)
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
                name=name,
                arguments=parsed,
                arguments_raw=raw,
                parse_error=parse_error,
            )
        )
    return calls


def _route_identity(config: ModelConfig) -> dict[str, Any]:
    return {
        "source": config.source,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
    }


def _route_usage(
    config: ModelConfig,
    usage: TokenUsage,
    *,
    accounting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {**_route_identity(config), **usage.to_dict()}
    if not accounting:
        return result
    reported_cost = _nonnegative_float(accounting.get("provider_reported_cost_usd"))
    reported_calls = int(accounting.get("provider_cost_reported_calls") or 0)
    missing_calls = int(accounting.get("provider_cost_missing_calls") or 0)
    if not reported_calls and not missing_calls:
        reported_calls = 1 if reported_cost is not None else 0
        missing_calls = 1 if reported_cost is None else 0
    result.update(
        {
            "provider_reported_cost_usd": reported_cost or 0.0,
            "upstream_inference_cost_usd": _nonnegative_float(
                accounting.get("upstream_inference_cost_usd")
            )
            or 0.0,
            "upstream_inference_prompt_cost_usd": _nonnegative_float(
                accounting.get("upstream_inference_prompt_cost_usd")
            )
            or 0.0,
            "upstream_inference_completions_cost_usd": _nonnegative_float(
                accounting.get("upstream_inference_completions_cost_usd")
            )
            or 0.0,
            "provider_cost_reported_calls": reported_calls,
            "provider_cost_missing_calls": missing_calls,
        }
    )
    return result


def _tag_attempts(
    attempts: list[dict[str, Any]],
    *,
    route: dict[str, Any],
) -> list[dict[str, Any]]:
    return [{**route, **dict(item)} for item in attempts]


def _with_route_index(
    records: list[dict[str, Any]],
    *,
    route_index: int,
) -> list[dict[str, Any]]:
    return [{**dict(item), "route_index": route_index} for item in records]


def _merge_route_usage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    integer_fields = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
        "total_tokens",
        "api_calls",
        "provider_cost_reported_calls",
        "provider_cost_missing_calls",
    )
    money_fields = (
        "provider_reported_cost_usd",
        "upstream_inference_cost_usd",
        "upstream_inference_prompt_cost_usd",
        "upstream_inference_completions_cost_usd",
    )
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for raw in records:
        record = dict(raw)
        key = (
            record.get("route_index"),
            str(record.get("source", "") or ""),
            str(record.get("provider", "") or ""),
            str(record.get("model", "") or ""),
            str(record.get("base_url", "") or ""),
        )
        if key not in merged:
            merged[key] = {
                "route_index": record.get("route_index"),
                "source": key[1],
                "provider": key[2],
                "model": key[3],
                "base_url": key[4],
                **{field: 0 for field in integer_fields},
                **{field: 0.0 for field in money_fields},
            }
            order.append(key)
        for field in integer_fields:
            merged[key][field] += max(0, int(record.get(field, 0) or 0))
        for field in money_fields:
            merged[key][field] += _nonnegative_float(record.get(field)) or 0.0
    return [merged[key] for key in order]


def _provider_accounting(
    config: ModelConfig,
    data: dict[str, Any],
    *,
    require_cost_status: bool = False,
) -> dict[str, Any]:
    """Extract secret-free provider billing fields from one successful call."""

    if config.provider.strip().casefold() != "openrouter":
        return {}
    usage_value = data.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    details = (
        usage.get("cost_details")
        if isinstance(usage.get("cost_details"), dict)
        else {}
    )
    reported_cost = _nonnegative_float(usage.get("cost"))
    generation_id = str(data.get("id") or "").strip()
    if not require_cost_status and not generation_id and not isinstance(usage_value, dict):
        return {}
    return {
        "generation_id": generation_id or None,
        "provider_cost_status": "reported" if reported_cost is not None else "missing",
        "provider_cost_source": (
            "chat_response_usage" if reported_cost is not None else None
        ),
        "provider_reported_cost_usd": reported_cost,
        "upstream_inference_cost_usd": _nonnegative_float(
            details.get("upstream_inference_cost")
        ),
        "upstream_inference_prompt_cost_usd": _nonnegative_float(
            details.get("upstream_inference_prompt_cost")
        ),
        "upstream_inference_completions_cost_usd": _nonnegative_float(
            details.get("upstream_inference_completions_cost")
        ),
        "is_byok": usage.get("is_byok") if isinstance(usage.get("is_byok"), bool) else None,
    }


def _aggregate_attempt_accounting(
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    money_fields = (
        "provider_reported_cost_usd",
        "upstream_inference_cost_usd",
        "upstream_inference_prompt_cost_usd",
        "upstream_inference_completions_cost_usd",
    )
    result: dict[str, Any] = {
        **{field: 0.0 for field in money_fields},
        "provider_cost_reported_calls": 0,
        "provider_cost_missing_calls": 0,
    }
    for attempt in attempts:
        status = str(attempt.get("provider_cost_status") or "").strip().casefold()
        if status == "reported":
            result["provider_cost_reported_calls"] += 1
        elif status == "missing":
            result["provider_cost_missing_calls"] += 1
        for field in money_fields:
            result[field] += _nonnegative_float(attempt.get(field)) or 0.0
    return result


def _aggregate_attempt_usage(attempts: list[dict[str, Any]]) -> TokenUsage:
    result = TokenUsage()
    for attempt in attempts:
        response_usage = attempt.get("response_usage")
        if isinstance(response_usage, dict):
            result.add(_normalize_usage(response_usage))
    result.api_calls = len(attempts)
    return result


def _mixed_route(usage_by_route: list[dict[str, Any]]) -> bool:
    attempted = {
        (
            item.get("route_index"),
            item.get("source"),
            item.get("provider"),
            item.get("model"),
            item.get("base_url"),
        )
        for item in usage_by_route
        if int(item.get("api_calls", 0) or 0) > 0
    }
    return len(attempted) > 1


def _normalize_usage(value: Any) -> TokenUsage:
    usage = value if isinstance(value, dict) else {}
    input_tokens = _first_int(usage, "prompt_tokens", "input_tokens")
    output_tokens = _first_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _first_int(usage, "total_tokens")
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    completion_details = (
        usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    )
    cached = _first_int(usage, "cached_input_tokens") or _first_int(
        prompt_details if isinstance(prompt_details, dict) else {},
        "cached_tokens",
    )
    reasoning = _first_int(usage, "reasoning_tokens") or _first_int(
        completion_details if isinstance(completion_details, dict) else {},
        "reasoning_tokens",
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        cached_input_tokens=cached,
        total_tokens=total_tokens,
        api_calls=1,
    )


def _first_int(value: dict[str, Any], *keys: str) -> int:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


def _nonnegative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
