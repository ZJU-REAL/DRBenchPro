"""Configuration parsing and bounds for the Local Research Framework."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


CANONICAL_PROTOCOL = "local_research_v1"
SUPPORTED_PROTOCOLS = {CANONICAL_PROTOCOL}
MODEL_ROUTE_IDENTITY_FIELDS = (
    "source",
    "provider",
    "model",
    "base_url",
    "api_key_env",
)
FORBIDDEN_SERVICE_ROUTE_OVERRIDE_KEYS = {
    "api_key",
    "api_key_env",
    "authorization",
    "base_url",
    "endpoint",
    "headers",
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "model_id",
    "provider",
    "stream",
    "tool_choice",
    "tools",
    "url",
}
ALLOWED_SERVICE_ROUTE_REQUEST_KEYS = {
    *MODEL_ROUTE_IDENTITY_FIELDS,
    "credential_label",
    "disable_env_proxy",
    "omit_temperature",
    "request_overrides",
    "max_retries",
    "backoff_seconds",
    "max_backoff_seconds",
    "backoff_jitter_ratio",
}


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def normalize_model_base_url(value: object) -> str:
    """Normalize an HTTP(S) model endpoint for exact policy comparisons."""

    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid model route base_url {raw!r}.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Model route base_url may not contain credentials, query, or fragment."
        )
    hostname = parsed.hostname.casefold()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def model_route_identity(route: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Extract the credential-bound route identity used by service policy."""

    if not isinstance(route, dict):
        raise TypeError("Model route allowlist entries must be mappings.")
    source = str(route.get("source", "") or "").strip()
    provider = str(route.get("provider", "") or "").strip()
    model = str(route.get("model", "") or "").strip()
    api_key_env = str(route.get("api_key_env", "") or "").strip()
    base_url = normalize_model_base_url(route.get("base_url"))
    values = (source, provider, model, base_url, api_key_env)
    missing = [
        field_name
        for field_name, field_value in zip(MODEL_ROUTE_IDENTITY_FIELDS, values)
        if not field_value
    ]
    if missing:
        raise ValueError(f"Model route identity is missing {missing}.")
    return values


def model_route_policy_identity(
    route: dict[str, Any], *, server_allowlist_entry: bool = False
) -> tuple[str, str, str, str, str, bool, bool, str, int, float, float, float]:
    allowed_keys = set(ALLOWED_SERVICE_ROUTE_REQUEST_KEYS)
    if server_allowlist_entry:
        allowed_keys.add("model_profile")
    unknown = set(route) - allowed_keys
    if unknown:
        raise ValueError(
            "Strict model routes contain unsupported fields: "
            f"{sorted(str(key) for key in unknown)}."
        )
    disable_env_proxy = route.get("disable_env_proxy", True)
    if not isinstance(disable_env_proxy, bool):
        raise ValueError("Model route disable_env_proxy must be a boolean.")
    omit_temperature = route.get("omit_temperature", False)
    if not isinstance(omit_temperature, bool):
        raise ValueError("Model route omit_temperature must be a boolean.")
    overrides = route.get("request_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("Model route request_overrides must be an object.")
    forbidden_override_keys = FORBIDDEN_SERVICE_ROUTE_OVERRIDE_KEYS - {"provider"}
    forbidden = forbidden_override_keys.intersection(
        str(key).strip().casefold() for key in overrides
    )
    if forbidden:
        raise ValueError(
            "Model route request_overrides may not replace routed fields: "
            f"{sorted(forbidden)}."
        )
    return (
        *model_route_identity(route),
        disable_env_proxy,
        omit_temperature,
        json.dumps(overrides, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        min(10, max(0, int(route.get("max_retries", 10)))),
        max(0.0, float(route.get("backoff_seconds", 1.0))),
        max(0.0, float(route.get("max_backoff_seconds", 8.0))),
        min(1.0, max(0.0, float(route.get("backoff_jitter_ratio", 0.0)))),
    )


@dataclass(frozen=True)
class ResearchServicePolicy:
    """Server-owned allowlists for the shared Research API deployment."""

    strict: bool = False
    allowed_model_profiles: frozenset[str] = frozenset()
    allowed_model_routes: tuple[
        tuple[
            str,
            tuple[tuple[str, str, str, str, str, bool, bool, str, int, float, float, float], ...],
        ],
        ...,
    ] = ()
    allow_incomplete_index: bool = False

    @classmethod
    def from_values(
        cls,
        *,
        strict: bool = False,
        allowed_model_profiles: Iterable[object] | None = None,
        allowed_model_routes: Iterable[dict[str, Any]] | None = None,
        allow_incomplete_index: bool = False,
    ) -> "ResearchServicePolicy":
        profiles = frozenset(
            str(item or "").strip()
            for item in (allowed_model_profiles or [])
            if str(item or "").strip()
        )
        route_identities: dict[
            str, list[tuple[str, str, str, str, str, bool, bool, str, int, float, float, float]]
        ] = {}
        for item in allowed_model_routes or []:
            if not isinstance(item, dict):
                raise TypeError("Allowed model routes must be mappings.")
            model_profile = str(item.get("model_profile", "") or "").strip()
            if not model_profile:
                raise ValueError(
                    "Allowed model routes must declare model_profile so a report "
                    "profile cannot select another profile's provider route."
                )
            identity = model_route_policy_identity(
                item, server_allowlist_entry=True
            )
            profile_routes = route_identities.setdefault(model_profile, [])
            if identity in profile_routes:
                raise ValueError(
                    f"Duplicate allowed route for model_profile {model_profile!r}."
                )
            profile_routes.append(identity)
        routes = tuple(
            (profile, tuple(profile_routes))
            for profile, profile_routes in route_identities.items()
        )
        return cls(
            strict=bool(strict),
            allowed_model_profiles=profiles,
            allowed_model_routes=routes,
            allow_incomplete_index=bool(allow_incomplete_index),
        )

    def validate_startup(self, *, service_api_key: str, wiki_base_url: str) -> None:
        if not self.strict:
            return
        if not str(service_api_key or "").strip():
            raise ValueError("Strict Research service policy requires a service API key.")
        if not str(wiki_base_url or "").strip():
            raise ValueError("Strict Research service policy requires wiki_base_url.")
        if not self.allowed_model_profiles:
            raise ValueError(
                "Strict Research service policy requires allowed_model_profiles."
            )
        if not self.allowed_model_routes:
            raise ValueError(
                "Strict Research service policy requires allowed_model_routes."
            )

    def validate_payload(self, payload: dict[str, Any]) -> None:
        if not self.strict:
            return
        engine_options = payload.get("engine_options")
        if not isinstance(engine_options, dict):
            raise ValueError("engine_options must be an object under strict policy.")
        if engine_options.get("offline_search_only") is not True:
            raise ValueError(
                "engine_options.offline_search_only must be true under strict policy."
            )
        if engine_options.get("fallback_policy") != "provider_errors_only":
            raise ValueError(
                "engine_options.fallback_policy must be provider_errors_only under "
                "strict policy."
            )
        model = payload.get("model")
        if not isinstance(model, dict):
            raise ValueError("model must be an object under strict service policy.")
        requested = str(model.get("requested", "") or "").strip()
        if requested not in self.allowed_model_profiles:
            raise ValueError(f"Requested model profile {requested!r} is not allowed.")
        routes = model.get("route_candidates")
        if not isinstance(routes, list) or not routes:
            raise ValueError(
                "model.route_candidates must be a non-empty list under strict policy."
            )
        requested_route_identities = []
        for index, route in enumerate(routes, start=1):
            if not isinstance(route, dict):
                raise ValueError(f"model.route_candidates[{index}] must be an object.")
            if str(route.get("api_key", "") or "").strip():
                raise ValueError(
                    f"model.route_candidates[{index}] embeds api_key; use an allowed "
                    "api_key_env."
                )
            if str(route.get("model_id", "") or "").strip():
                raise ValueError(
                    f"model.route_candidates[{index}] may not set model_id; the "
                    "allowlisted model field is authoritative."
                )
            for container_name in ("request_overrides", "extra_body"):
                overrides = route.get(container_name) or {}
                if not isinstance(overrides, dict):
                    raise ValueError(
                        f"model.route_candidates[{index}].{container_name} must be "
                        "an object."
                    )
                forbidden_override_keys = FORBIDDEN_SERVICE_ROUTE_OVERRIDE_KEYS - {
                    "provider"
                }
                forbidden = forbidden_override_keys.intersection(
                    str(key).strip().casefold() for key in overrides
                )
                if forbidden:
                    raise ValueError(
                        f"model.route_candidates[{index}].{container_name} may not "
                        f"override server-routed fields: {sorted(forbidden)}."
                    )
            identity = model_route_policy_identity(route)
            requested_route_identities.append(identity)
        allowed_sequence = dict(self.allowed_model_routes).get(requested)
        if allowed_sequence != tuple(requested_route_identities):
            rendered = [
                {
                    **dict(zip(MODEL_ROUTE_IDENTITY_FIELDS, identity[:5])),
                    "disable_env_proxy": identity[5],
                    "omit_temperature": identity[6],
                    "request_overrides": json.loads(identity[7]),
                    "max_retries": identity[8],
                    "backoff_seconds": identity[9],
                    "max_backoff_seconds": identity[10],
                    "backoff_jitter_ratio": identity[11],
                }
                for identity in requested_route_identities
            ]
            raise ValueError(
                "model.route_candidates must exactly match the server-owned ordered "
                f"route sequence for profile {requested!r}: {rendered!r}."
            )


@dataclass
class LocalResearchConfig:
    contract_version: str = "local-wikipedia-research/v1"
    protocol: str = CANONICAL_PROTOCOL
    snapshot_id: str = "enwiki-20260701-v1"
    index_id: str = ""
    model_profile: str = ""
    model: dict[str, Any] = field(default_factory=dict)
    model_routes: list[dict[str, Any]] = field(default_factory=list)
    wiki: dict[str, Any] = field(default_factory=dict)
    protocol_config: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    citation_required: bool = True
    allow_incomplete_index: bool = False
    fallback_policy: str = "provider_errors_only"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "LocalResearchConfig":
        if not isinstance(value, dict):
            raise TypeError("LocalResearchEngine config must be a mapping.")
        supplied_contract = str(
            value.get("schema_version") or value.get("contract_version") or ""
        ).strip()
        if supplied_contract and supplied_contract != "local-wikipedia-research/v1":
            raise ValueError(
                f"Unsupported research contract {supplied_contract!r}; expected "
                "'local-wikipedia-research/v1'."
            )
        protocol = str(value.get("protocol", CANONICAL_PROTOCOL) or "").strip().lower()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                f"Unsupported local research protocol {protocol!r}; "
                f"expected one of {sorted(SUPPORTED_PROTOCOLS)}."
            )
        knowledge_base = value.get("knowledge_base") or {}
        request_model = value.get("model") or {}
        is_http_contract = isinstance(request_model, dict) and (
            "route_candidates" in request_model or "requested" in request_model
        )
        routes = (
            request_model.get("route_candidates")
            if is_http_contract
            else value.get("model_routes")
        ) or []
        if not isinstance(routes, list):
            raise TypeError("model_routes must be an ordered list of mappings.")
        if not all(isinstance(item, dict) for item in routes):
            raise TypeError("Every model_routes entry must be a mapping.")
        model = {} if is_http_contract else value.get("model") or {}
        wiki = value.get("wiki") or {
            "base_url": str(
                value.get("wiki_base_url")
                or os.getenv("LOCAL_WIKIPEDIA_SEARCH_URL", "")
                or "http://127.0.0.1:8091"
            )
        }
        protocol_config = value.get("protocol_config") or value.get("limits") or {}
        if not isinstance(protocol_config, dict):
            raise TypeError("protocol_config/limits must be a mapping.")
        protocol_config = dict(protocol_config)
        initial_units = protocol_config.get("initial_research_units")
        if initial_units is not None:
            protocol_config.setdefault("initial_research_units_min", initial_units)
            protocol_config.setdefault("initial_research_units_max", initial_units)
        if "max_gap_checks" in protocol_config:
            protocol_config.setdefault(
                "gap_check_enabled", int(protocol_config.get("max_gap_checks") or 0) > 0
            )
        max_tokens = protocol_config.get("max_model_output_tokens_per_call")
        normalized_routes = [dict(item) for item in routes]
        if max_tokens is not None:
            for route in normalized_routes:
                route.setdefault("max_tokens", max_tokens)
        report = value.get("report") or {}
        engine_options = value.get("engine_options") or {}
        if not isinstance(engine_options, dict):
            raise TypeError("engine_options must be a mapping.")
        fallback_policy = str(
            engine_options.get("fallback_policy", "provider_errors_only") or ""
        ).strip()
        if fallback_policy not in {"provider_errors_only", "all_model_errors"}:
            raise ValueError(
                "engine_options.fallback_policy must be provider_errors_only or "
                "all_model_errors."
            )
        if not isinstance(model, dict) or not isinstance(wiki, dict):
            raise TypeError("model and wiki must be mappings.")
        return cls(
            contract_version=str(
                value.get("schema_version")
                or value.get("contract_version")
                or "local-wikipedia-research/v1"
            ),
            protocol=protocol,
            snapshot_id=str(
                (knowledge_base.get("snapshot_id") if isinstance(knowledge_base, dict) else "")
                or value.get("snapshot_id", "enwiki-20260701-v1")
                or ""
            ),
            index_id=str(
                (knowledge_base.get("index_id") if isinstance(knowledge_base, dict) else "")
                or value.get("index_id", "")
                or ""
            ),
            model_profile=str(
                (request_model.get("requested") if is_http_contract else "")
                or value.get("model_profile", "")
                or ""
            ),
            model=dict(model),
            model_routes=normalized_routes,
            wiki=dict(wiki),
            protocol_config=protocol_config,
            report=dict(report) if isinstance(report, dict) else {},
            citation_required=bool(
                engine_options.get("require_citations", value.get("citation_required", True))
                if isinstance(engine_options, dict)
                else value.get("citation_required", True)
            ),
            allow_incomplete_index=bool(
                wiki.get("allow_incomplete_index", False)
                if isinstance(wiki, dict)
                else False
            ),
            fallback_policy=fallback_policy,
        )

    @property
    def initial_unit_min(self) -> int:
        return _bounded_int(
            self.protocol_config.get("initial_research_units_min"),
            default=3,
            minimum=3,
            maximum=5,
        )

    @property
    def initial_unit_max(self) -> int:
        raw = _bounded_int(
            self.protocol_config.get("initial_research_units_max"),
            default=5,
            minimum=3,
            maximum=5,
        )
        return max(self.initial_unit_min, raw)

    @property
    def max_research_units(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_research_units"),
            default=6,
            minimum=self.initial_unit_max,
            maximum=6,
        )

    @property
    def rounds_per_unit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_rounds_per_unit"),
            default=4,
            # Five rounds preserve the original Search2/Open2-style schedule;
            # seven rounds allow the isolated Search3/Open3 exploration to
            # reserve its final round for evidence notes.
            minimum=3,
            maximum=7,
        )

    @property
    def tool_calls_per_unit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_tool_calls_per_unit"),
            default=8,
            minimum=1,
            maximum=25,
        )

    @property
    def search_calls_per_unit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_search_calls_per_unit"),
            default=2,
            minimum=1,
            maximum=10,
        )

    @property
    def open_calls_per_turn(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_open_calls_per_turn"),
            default=3,
            minimum=1,
            maximum=10,
        )

    @property
    def open_calls_per_unit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_open_calls_per_unit"),
            default=6,
            minimum=1,
            maximum=20,
        )

    @property
    def open_chars_per_call(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_open_chars_per_call"),
            default=6000,
            minimum=128,
            maximum=12000,
        )

    @property
    def open_chars_per_unit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_open_chars_per_unit"),
            default=30000,
            minimum=128,
            maximum=120000,
        )

    @property
    def initial_units_tool_limit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_initial_units_tool_calls"),
            default=24,
            minimum=1,
            maximum=200,
        )

    @property
    def gap_unit_tool_limit(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_gap_unit_tool_calls"),
            default=4,
            minimum=1,
            maximum=50,
        )

    @property
    def max_total_tool_calls(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_total_tool_calls"),
            default=self.max_research_units * self.tool_calls_per_unit,
            minimum=1,
            maximum=200,
        )

    def per_tool_limit(self, name: str) -> int | None:
        raw = self.protocol_config.get(f"max_{name}_calls")
        if raw is None:
            return None
        return _bounded_int(raw, default=1, minimum=0, maximum=200)

    @property
    def unit_concurrency(self) -> int:
        return _bounded_int(
            self.protocol_config.get("unit_concurrency"),
            default=3,
            minimum=1,
            maximum=6,
        )

    @property
    def unit_retry_attempts(self) -> int:
        """Number of fresh bounded replacements after an error/incomplete Unit."""

        return _bounded_int(
            self.protocol_config.get("max_unit_retries"),
            default=1,
            minimum=0,
            maximum=2,
        )

    @property
    def require_all_units_grounded(self) -> bool:
        """Prevent partial-evidence synthesis from being labeled successful."""

        return bool(self.protocol_config.get("require_all_units_grounded", True))

    @property
    def gap_check_enabled(self) -> bool:
        return bool(self.protocol_config.get("gap_check_enabled", True))

    @property
    def synthesis_evidence_chars(self) -> int:
        return _bounded_int(
            self.protocol_config.get("synthesis_evidence_chars"),
            default=80000,
            minimum=4000,
            maximum=200000,
        )

    @property
    def research_output_tokens(self) -> int:
        return _bounded_int(
            self.protocol_config.get("research_output_tokens"),
            default=8192,
            minimum=128,
            maximum=65536,
        )

    @property
    def summary_output_tokens(self) -> int:
        return _bounded_int(
            self.protocol_config.get("summary_output_tokens"),
            default=8192,
            minimum=128,
            maximum=65536,
        )

    @property
    def gap_evidence_chars(self) -> int:
        return _bounded_int(
            self.protocol_config.get("gap_evidence_chars"),
            default=48000,
            minimum=4000,
            maximum=200000,
        )

    @property
    def model_output_tokens(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_model_output_tokens_per_call"),
            default=4096,
            minimum=128,
            maximum=65536,
        )

    @property
    def final_report_tokens(self) -> int:
        return _bounded_int(
            self.protocol_config.get("final_report_max_tokens"),
            default=6000,
            minimum=128,
            maximum=65536,
        )

    @property
    def final_report_attempts(self) -> int:
        return _bounded_int(
            self.protocol_config.get("max_final_report_attempts"),
            default=3,
            minimum=1,
            maximum=3,
        )

    @property
    def report_min_words(self) -> int:
        return _bounded_int(
            self.report.get("min_words"),
            default=0,
            minimum=0,
            maximum=50000,
        )
