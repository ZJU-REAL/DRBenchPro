"""Thread-safe telemetry for Local Research Framework runs."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from typing import Any, Callable

from .schemas import (
    ChatResponse,
    StageMetrics,
    TokenUsage,
    ToolCall,
    ToolUsage,
    TraceEvent,
    utc_now_iso,
)
from .wiki_client import WikiTools, extract_source_ids


TelemetryObserver = Callable[[str, dict[str, Any]], None]


class RunRecorder:
    def __init__(
        self,
        *,
        protocol: str,
        observer: TelemetryObserver | None = None,
    ):
        self.protocol = protocol
        self.usage = TokenUsage()
        self.tool_usage = ToolUsage()
        self.trace: list[TraceEvent] = []
        self.model_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.stage_metrics: dict[str, StageMetrics] = defaultdict(StageMetrics)
        self.routes_used: list[dict[str, Any]] = []
        self._reserved_tool_calls = 0
        self._reserved_tool_calls_by_name: dict[str, int] = defaultdict(int)
        self._reserved_tool_calls_by_scope: dict[str, int] = defaultdict(int)
        self._sequence = 0
        self._lock = threading.Lock()
        self._observer = observer

    def _observe(self, record_type: str, payload: dict[str, Any]) -> None:
        if self._observer is not None:
            self._observer(record_type, payload)

    def event(
        self,
        event_type: str,
        phase: str,
        *,
        round_index: int | None = None,
        research_unit_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> TraceEvent:
        with self._lock:
            self._sequence += 1
            event = TraceEvent(
                sequence=self._sequence,
                timestamp_utc=utc_now_iso(),
                event_type=event_type,
                phase=phase,
                round_index=round_index,
                research_unit_id=research_unit_id,
                details=details or {},
            )
            self.trace.append(event)
        self._observe("trace_event", event.to_dict())
        return event

    def reserve_tool_call(
        self,
        name: str,
        *,
        total_limit: int,
        per_tool_limit: int | None = None,
        scope: str = "",
        scope_limit: int | None = None,
    ) -> bool:
        """Atomically reserve one execution slot across concurrent units."""

        with self._lock:
            if self._reserved_tool_calls >= total_limit:
                return False
            if (
                per_tool_limit is not None
                and self._reserved_tool_calls_by_name[name] >= per_tool_limit
            ):
                return False
            if (
                scope
                and scope_limit is not None
                and self._reserved_tool_calls_by_scope[scope] >= scope_limit
            ):
                return False
            self._reserved_tool_calls += 1
            self._reserved_tool_calls_by_name[name] += 1
            if scope:
                self._reserved_tool_calls_by_scope[scope] += 1
            return True

    def record_model(
        self,
        response: ChatResponse,
        *,
        phase: str,
        round_index: int | None = None,
        research_unit_id: str = "",
    ) -> None:
        with self._lock:
            call_index = len(self.model_calls) + 1
            self.usage.add(response.usage)
            metrics = self.stage_metrics[phase]
            metrics.usage.add(response.usage)
            metrics.model_calls += 1
            metrics.model_latency_seconds += response.latency_seconds
            route = dict(response.route)
            metrics.add_route_accounting(route.get("usage_by_route") or [])
            if route:
                raw_usage_by_route = route.get("usage_by_route")
                if not isinstance(raw_usage_by_route, list) or not raw_usage_by_route:
                    route["usage_by_route"] = [
                        {
                            **_route_identity(route),
                            **response.usage.to_dict(),
                        }
                    ]
                route["route_attempts"] = [
                    {
                        **dict(item),
                        "model_call_index": call_index,
                        "phase": phase,
                        "round_index": round_index,
                        "research_unit_id": research_unit_id,
                    }
                    for item in route.get("route_attempts") or []
                    if isinstance(item, dict)
                ]
                self.routes_used.append(route)
            model_record = {
                    "call_index": call_index,
                    "phase": phase,
                    "round_index": round_index,
                    "research_unit_id": research_unit_id,
                    "request": response.request_payload,
                    "response": response.raw_response,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage.to_dict(),
                    "latency_seconds": round(response.latency_seconds, 6),
                    "route": route,
                }
            model_record["cost"] = _call_cost(route, response.usage.api_calls)
            self.model_calls.append(model_record)
        self._observe("model_call", model_record)
        self.event(
            "model_response",
            phase,
            round_index=round_index,
            research_unit_id=research_unit_id,
            details={
                "model_call_index": call_index,
                "finish_reason": response.finish_reason,
                "content_chars": len(response.content),
                "tool_call_count": len(response.tool_calls),
                "usage": response.usage.to_dict(),
                "latency_seconds": round(response.latency_seconds, 6),
                "route": route,
            },
        )

    def record_model_error(
        self,
        error: Exception,
        *,
        phase: str = "research",
        round_index: int | None = None,
        research_unit_id: str = "",
    ) -> None:
        attempts = [
            dict(item)
            for item in (getattr(error, "attempts", None) or [])
            if isinstance(item, dict)
        ]
        usage_by_route = [
            dict(item)
            for item in (getattr(error, "usage_by_route", None) or [])
            if isinstance(item, dict)
        ]
        api_calls = sum(int(item.get("api_calls", 0) or 0) for item in usage_by_route)
        if not api_calls:
            api_calls = len(attempts)
        failed_usage = TokenUsage(api_calls=api_calls)
        latency = sum(float(item.get("latency_seconds", 0) or 0) for item in attempts)
        with self._lock:
            call_index = len(self.model_calls) + 1
            self.usage.add(failed_usage)
            metrics = self.stage_metrics[phase]
            metrics.usage.add(failed_usage)
            metrics.model_calls += 1
            metrics.model_latency_seconds += latency
            metrics.add_route_accounting(usage_by_route)
            annotated_attempts = [
                {
                    **item,
                    "model_call_index": call_index,
                    "phase": phase,
                    "round_index": round_index,
                    "research_unit_id": research_unit_id,
                }
                for item in attempts
            ]
            route = {
                "source": "",
                "provider": "",
                "model": "",
                "base_url": "",
                "fallback_used": len(
                    {
                        item.get("route_index")
                        for item in usage_by_route
                        if int(item.get("api_calls", 0) or 0) > 0
                    }
                )
                > 1,
                "mixed_route": _mixed_route(usage_by_route),
                "usage_by_route": usage_by_route,
                "route_attempts": annotated_attempts,
                "route_failures": [
                    dict(item)
                    for item in (getattr(error, "route_failures", None) or [])
                    if isinstance(item, dict)
                ],
            }
            self.routes_used.append(route)
            model_record = {
                    "call_index": call_index,
                    "phase": phase,
                    "round_index": round_index,
                    "research_unit_id": research_unit_id,
                    "status": "error",
                    "request": None,
                    "response": None,
                    "finish_reason": "",
                    "usage": failed_usage.to_dict(),
                    "latency_seconds": round(latency, 6),
                    "route": route,
                    "error": {
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    },
                }
            model_record["cost"] = _call_cost(route, failed_usage.api_calls)
            self.model_calls.append(model_record)
        self._observe("model_call", model_record)
        self.event(
            "model_error",
            phase,
            round_index=round_index,
            research_unit_id=research_unit_id,
            details={
                "model_call_index": call_index,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "usage": failed_usage.to_dict(),
                "latency_seconds": round(latency, 6),
                "route": route,
            },
        )

    def execute_tool(
        self,
        tools: WikiTools,
        call: ToolCall,
        *,
        phase: str,
        round_index: int,
        research_unit_id: str = "",
    ) -> tuple[dict[str, Any], bool, list[str]]:
        started = time.perf_counter()
        success = False
        error = ""
        output: dict[str, Any]
        if call.parse_error:
            output = {
                "error": "invalid_tool_arguments",
                "message": call.parse_error,
                "arguments_raw": call.arguments_raw,
            }
        else:
            try:
                output = tools.execute(call.name, call.arguments)
                success = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                output = {"error": "tool_execution_failed", "message": error}
        elapsed = time.perf_counter() - started
        source_ids = extract_source_ids(output) if success else []
        with self._lock:
            tool_call_index = len(self.tool_calls) + 1
            self.tool_usage.record(call.name, success=success, source_ids=source_ids)
            metrics = self.stage_metrics[phase]
            metrics.tool_calls += 1
            metrics.tool_latency_seconds += elapsed
            record = {
                "tool_call_index": tool_call_index,
                "phase": phase,
                "round_index": round_index,
                "research_unit_id": research_unit_id,
                "tool_call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "arguments_raw": call.arguments_raw,
                "success": success,
                "error": error or (str(output.get("message", "")) if not success else ""),
                "response": output,
                "source_ids": source_ids,
                "latency_seconds": round(elapsed, 6),
            }
            self.tool_calls.append(record)
        self._observe("tool_call", record)
        self.event(
            "tool_result",
            phase,
            round_index=round_index,
            research_unit_id=research_unit_id,
            details=record,
        )
        return output, success, source_ids

    def raw_response(self) -> dict[str, Any]:
        with self._lock:
            return {
                "protocol": self.protocol,
                "model_calls": list(self.model_calls),
                "tool_calls": list(self.tool_calls),
            }

    def route_summary(self) -> dict[str, Any]:
        with self._lock:
            routes = list(self.routes_used)
        usage_by_route = _merge_route_usage(
            [
                dict(item)
                for route in routes
                for item in (route.get("usage_by_route") or [])
                if isinstance(item, dict)
            ]
        )
        attempts = [
            dict(item)
            for route in routes
            for item in (route.get("route_attempts") or [])
            if isinstance(item, dict)
        ]
        failures = [
            dict(item)
            for route in routes
            for item in (route.get("route_failures") or [])
            if isinstance(item, dict)
        ]
        selected = max(
            (route for route in routes if str(route.get("source", "") or "")),
            key=lambda route: int(route.get("route_index", 0) or 0),
            default={},
        )
        fallback_used = any(bool(route.get("fallback_used")) for route in routes)
        return {
            "source": str(selected.get("source", "") or ""),
            "provider": str(selected.get("provider", "") or ""),
            "model": str(selected.get("model", "") or ""),
            "base_url": str(selected.get("base_url", "") or ""),
            "fallback_used": fallback_used,
            "fallback_source": (
                str(selected.get("source", "") or "")
                if fallback_used
                else None
            ),
            "mixed_route": _mixed_route(usage_by_route),
            "usage_by_route": usage_by_route,
            "api_attempts": attempts,
            "route_failures": failures,
            "routes_used": routes,
        }


def _call_cost(route: dict[str, Any], api_calls: int) -> dict[str, Any]:
    usage_records = [
        item
        for item in (route.get("usage_by_route") or [])
        if isinstance(item, dict)
    ]
    reported_calls = sum(
        int(item.get("provider_cost_reported_calls", 0) or 0)
        for item in usage_records
    )
    missing_calls = sum(
        int(item.get("provider_cost_missing_calls", 0) or 0)
        for item in usage_records
    )
    accounted_calls = reported_calls + missing_calls
    if accounted_calls < int(api_calls or 0):
        missing_calls += int(api_calls or 0) - accounted_calls
    return {
        "currency": "USD",
        "provider_reported_cost_usd": round(
            sum(
                float(item.get("provider_reported_cost_usd", 0.0) or 0.0)
                for item in usage_records
            ),
            12,
        ),
        "upstream_inference_cost_usd": round(
            sum(
                float(item.get("upstream_inference_cost_usd", 0.0) or 0.0)
                for item in usage_records
            ),
            12,
        ),
        "upstream_inference_prompt_cost_usd": round(
            sum(
                float(item.get("upstream_inference_prompt_cost_usd", 0.0) or 0.0)
                for item in usage_records
            ),
            12,
        ),
        "upstream_inference_completions_cost_usd": round(
            sum(
                float(
                    item.get("upstream_inference_completions_cost_usd", 0.0) or 0.0
                )
                for item in usage_records
            ),
            12,
        ),
        "provider_cost_reported_calls": reported_calls,
        "provider_cost_missing_calls": missing_calls,
        "provider_cost_complete": reported_calls > 0 and missing_calls == 0,
        "cost_missing": missing_calls > 0,
    }


def assistant_message(response: ChatResponse) -> dict[str, Any]:
    message: dict[str, Any] = {}
    choices = response.raw_response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        raw_message = choices[0].get("message")
        if isinstance(raw_message, dict):
            # Preserve provider-returned reasoning fields/signatures required by
            # some thinking models when continuing after a tool call.
            message.update(raw_message)
    message["role"] = "assistant"
    message["content"] = response.content or None
    if response.tool_calls:
        raw_tool_calls = message.get("tool_calls")
        if not isinstance(raw_tool_calls, list):
            message["tool_calls"] = [
                call.to_openai_dict() for call in response.tool_calls
            ]
    return message


def tool_message(call: ToolCall, output: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps(output, ensure_ascii=False, separators=(",", ":")),
    }


def _route_identity(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route_index": route.get("route_index"),
        "source": str(route.get("source", "") or ""),
        "provider": str(route.get("provider", "") or ""),
        "model": str(route.get("model", "") or ""),
        "base_url": str(route.get("base_url", "") or ""),
    }


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
    for record in records:
        identity = _route_identity(record)
        key = tuple(identity[field] for field in identity)
        if key not in merged:
            merged[key] = {
                **identity,
                **{field: 0 for field in integer_fields},
                **{field: 0.0 for field in money_fields},
            }
            order.append(key)
        for field in integer_fields:
            merged[key][field] += max(0, int(record.get(field, 0) or 0))
        for field in money_fields:
            try:
                value = float(record.get(field, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            merged[key][field] += max(0.0, value)
    return [merged[key] for key in order]


def _mixed_route(usage_by_route: list[dict[str, Any]]) -> bool:
    return (
        len(
            {
                tuple(_route_identity(item).values())
                for item in usage_by_route
                if int(item.get("api_calls", 0) or 0) > 0
            }
        )
        > 1
    )
