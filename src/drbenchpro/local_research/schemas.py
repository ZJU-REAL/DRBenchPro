"""Serializable contracts for local-Wikipedia research runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TokenUsage:
    """Provider-reported token usage accumulated across model calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    api_calls: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += int(other.input_tokens)
        self.output_tokens += int(other.output_tokens)
        self.reasoning_tokens += int(other.reasoning_tokens)
        self.cached_input_tokens += int(other.cached_input_tokens)
        self.total_tokens += int(other.total_tokens)
        self.api_calls += int(other.api_calls)

    def copy(self) -> "TokenUsage":
        return TokenUsage(**asdict(self))

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    arguments_raw: str = ""
    parse_error: str = ""

    def to_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments_raw,
            },
        }


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: TokenUsage
    raw_response: dict[str, Any]
    request_payload: dict[str, Any]
    finish_reason: str = ""
    model: str = ""
    provider: str = ""
    latency_seconds: float = 0.0
    route: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceEvent:
    sequence: int
    timestamp_utc: str
    event_type: str
    phase: str
    round_index: int | None = None
    research_unit_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolUsage:
    search_calls: int = 0
    open_calls: int = 0
    find_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    retrieved_source_ids: list[str] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return self.search_calls + self.open_calls + self.find_calls

    def record(self, name: str, *, success: bool, source_ids: list[str]) -> None:
        if name == "search":
            self.search_calls += 1
        elif name == "open":
            self.open_calls += 1
        elif name == "find":
            self.find_calls += 1
        if success:
            self.successful_calls += 1
        else:
            self.failed_calls += 1
        seen = set(self.retrieved_source_ids)
        for source_id in source_ids:
            if source_id and source_id not in seen:
                self.retrieved_source_ids.append(source_id)
                seen.add(source_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "search": self.search_calls,
            "open": self.open_calls,
            "find": self.find_calls,
            "total": self.total_calls,
            "successful": self.successful_calls,
            "failed": self.failed_calls,
            "unique_source_count": len(self.retrieved_source_ids),
            "retrieved_source_ids": list(self.retrieved_source_ids),
        }


@dataclass
class CitationValidation:
    cited_source_ids: list[str] = field(default_factory=list)
    valid_source_ids: list[str] = field(default_factory=list)
    invalid_source_ids: list[str] = field(default_factory=list)
    retrieved_source_ids: list[str] = field(default_factory=list)
    citation_required: bool = True

    @property
    def valid(self) -> bool:
        if self.invalid_source_ids:
            return False
        return bool(self.cited_source_ids) or not self.citation_required

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "citation_count": len(self.cited_source_ids),
            "valid_citation_count": len(self.valid_source_ids),
            "invalid_citation_count": len(self.invalid_source_ids),
            "missing_citations": self.citation_required and not self.cited_source_ids,
            "valid": self.valid,
        }


@dataclass
class StageMetrics:
    usage: TokenUsage = field(default_factory=TokenUsage)
    model_latency_seconds: float = 0.0
    tool_latency_seconds: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    provider_reported_cost_usd: float = 0.0
    upstream_inference_cost_usd: float = 0.0
    upstream_inference_prompt_cost_usd: float = 0.0
    upstream_inference_completions_cost_usd: float = 0.0
    provider_cost_reported_calls: int = 0
    provider_cost_missing_calls: int = 0

    def add_route_accounting(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            if not isinstance(record, dict):
                continue
            self.provider_reported_cost_usd += float(
                record.get("provider_reported_cost_usd", 0.0) or 0.0
            )
            self.upstream_inference_cost_usd += float(
                record.get("upstream_inference_cost_usd", 0.0) or 0.0
            )
            self.upstream_inference_prompt_cost_usd += float(
                record.get("upstream_inference_prompt_cost_usd", 0.0) or 0.0
            )
            self.upstream_inference_completions_cost_usd += float(
                record.get("upstream_inference_completions_cost_usd", 0.0) or 0.0
            )
            self.provider_cost_reported_calls += int(
                record.get("provider_cost_reported_calls", 0) or 0
            )
            self.provider_cost_missing_calls += int(
                record.get("provider_cost_missing_calls", 0) or 0
            )

    def cost_dict(self) -> dict[str, Any]:
        return {
            "provider_reported_cost_usd": round(
                self.provider_reported_cost_usd, 12
            ),
            "upstream_inference_cost_usd": round(
                self.upstream_inference_cost_usd, 12
            ),
            "upstream_inference_prompt_cost_usd": round(
                self.upstream_inference_prompt_cost_usd, 12
            ),
            "upstream_inference_completions_cost_usd": round(
                self.upstream_inference_completions_cost_usd, 12
            ),
            "provider_cost_reported_calls": self.provider_cost_reported_calls,
            "provider_cost_missing_calls": self.provider_cost_missing_calls,
            "provider_cost_complete": (
                self.provider_cost_reported_calls > 0
                and self.provider_cost_missing_calls == 0
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "usage": self.usage.to_dict(),
            "latency_seconds": round(
                self.model_latency_seconds + self.tool_latency_seconds, 6
            ),
            "model_latency_seconds": round(self.model_latency_seconds, 6),
            "tool_latency_seconds": round(self.tool_latency_seconds, 6),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "cost": self.cost_dict(),
        }


@dataclass
class ResearchUnitResult:
    unit_id: str
    title: str
    objective: str
    notes: str
    rounds: int
    tool_calls: int
    source_ids: list[str] = field(default_factory=list)
    status: str = "success"
    error: str = ""
    budget_usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchResult:
    """Stable engine result and HTTP response body contract."""

    contract_version: str
    status: str
    protocol: str
    case_id: str
    query: str
    snapshot_id: str
    index_id: str
    final_report: str
    raw_response: dict[str, Any]
    usage: TokenUsage
    tool_usage: ToolUsage
    trace: list[TraceEvent]
    citation_validation: CitationValidation
    route: dict[str, Any]
    stage_metrics: dict[str, StageMetrics]
    latency_seconds: float
    started_at_utc: str
    finished_at_utc: str
    run_id: str
    trace_id: str
    research_units: list[ResearchUnitResult] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "status": self.status,
            "protocol": self.protocol,
            "case_id": self.case_id,
            "query": self.query,
            "snapshot_id": self.snapshot_id,
            "index_id": self.index_id,
            "final_report": self.final_report,
            "raw_response": self.raw_response,
            "usage": self.usage.to_dict(),
            "tool_usage": self.tool_usage.to_dict(),
            "trace": [event.to_dict() for event in self.trace],
            "citation_validation": self.citation_validation.to_dict(),
            "route": self.route,
            "stage_metrics": {
                name: metrics.to_dict()
                for name, metrics in self.stage_metrics.items()
            },
            "latency_seconds": round(self.latency_seconds, 6),
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "research_units": [unit.to_dict() for unit in self.research_units],
            "error": self.error,
        }

    def to_api_response(self) -> dict[str, Any]:
        """Return the public `/v1/research` response shape."""

        usage = {
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "total_tokens": self.usage.total_tokens,
            "api_calls": self.usage.api_calls,
        }
        tool_counts = {
            "search": self.tool_usage.search_calls,
            "open": self.tool_usage.open_calls,
            "find": self.tool_usage.find_calls,
            "total": self.tool_usage.total_calls,
        }
        stages = []
        for name, metrics in self.stage_metrics.items():
            stage_usage = metrics.usage
            stages.append(
                {
                    "name": name,
                    "latency_seconds": round(
                        metrics.model_latency_seconds + metrics.tool_latency_seconds,
                        6,
                    ),
                    "usage": {
                        "input_tokens": stage_usage.input_tokens,
                        "output_tokens": stage_usage.output_tokens,
                        "reasoning_tokens": stage_usage.reasoning_tokens,
                        "cached_input_tokens": stage_usage.cached_input_tokens,
                        "total_tokens": stage_usage.total_tokens,
                        "api_calls": stage_usage.api_calls,
                    },
                    "model_calls": metrics.model_calls,
                    "tool_calls": metrics.tool_calls,
                    "model_latency_seconds": round(
                        metrics.model_latency_seconds, 6
                    ),
                    "tool_latency_seconds": round(
                        metrics.tool_latency_seconds, 6
                    ),
                    "cost": metrics.cost_dict(),
                }
            )
        return {
            "schema_version": self.contract_version,
            "status": self.status,
            "final_report": self.final_report,
            "metadata": {
                "protocol": self.protocol,
                "snapshot_id": self.snapshot_id,
                "index_id": self.index_id,
                "route": self.route,
                "latency_seconds": round(self.latency_seconds, 6),
                "stages": stages,
                "usage": usage,
                "tool_counts": tool_counts,
                "citation_validation": self.citation_validation.to_dict(),
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "research_units": [unit.to_dict() for unit in self.research_units],
            },
            "trace": {
                "events": [event.to_dict() for event in self.trace],
                "model_calls": self.raw_response.get("model_calls", []),
                "tool_calls": self.raw_response.get("tool_calls", []),
                "agent_metadata": self.raw_response.get("agent_metadata", {}),
            },
            "error": self.error,
        }
