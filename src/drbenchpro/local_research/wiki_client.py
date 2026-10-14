"""HTTP client and function schemas for the fixed Wikipedia snapshot tools."""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import requests

from .wikipedia.constants import (
    CANONICAL_INDEX_CHUNK_CHARS,
    CANONICAL_INDEX_CHUNK_OVERLAP,
    CANONICAL_INDEX_CLEANER,
    CANONICAL_INDEX_NAMESPACE,
    INDEX_BACKEND,
    MAX_FIND_PATTERN_CHARS,
    MAX_OPEN_CHARS,
    MAX_SEARCH_QUERY_CHARS,
    MAX_SOURCE_ID_CHARS,
    canonical_index_id,
    index_build_fingerprint,
    index_id_for_build,
)


class WikiToolError(RuntimeError):
    pass


class WikiToolContractError(WikiToolError):
    pass


class WikiTools(Protocol):
    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


WikiTransport = Callable[[str, dict[str, Any], float], dict[str, Any]]


WIKI_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search the fixed local English Wikipedia snapshot. Returns stable "
                "source_id values that can be passed to open or find."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SEARCH_QUERY_CHARS,
                    },
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open",
            "description": (
                "Open text from one source_id in the fixed snapshot. Use cursor "
                "and next_cursor to page through long content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SOURCE_ID_CHARS,
                    },
                    "cursor": {"type": "integer", "minimum": 0},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 128,
                        "maximum": MAX_OPEN_CHARS,
                    },
                },
                "required": ["source_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find",
            "description": (
                "Find literal text within one source_id and return matching context "
                "and cursors for open."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SOURCE_ID_CHARS,
                    },
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FIND_PATTERN_CHARS,
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["source_id", "pattern"],
                "additionalProperties": False,
            },
        },
    },
]


def wikipedia_tool_schemas(*, max_open_chars: int = 6000) -> list[dict[str, Any]]:
    """Return the framework's Search/Open tools while retaining backend ``find``."""

    bounded_open_chars = min(MAX_OPEN_CHARS, max(128, int(max_open_chars)))
    schemas = [
        copy.deepcopy(item)
        for item in WIKI_TOOL_SCHEMAS
        if item["function"]["name"] in {"search", "open"}
    ]
    for item in schemas:
        function = item["function"]
        if function["name"] == "search":
            function["description"] = (
                "Search the fixed local English Wikipedia snapshot. Results include "
                "a stable source_id, the matched chunk offsets, and a suggested_cursor "
                "for open."
            )
        elif function["name"] == "open":
            function["parameters"]["properties"]["max_chars"]["maximum"] = (
                bounded_open_chars
            )
    return schemas


@dataclass
class WikiClientConfig:
    base_url: str
    snapshot_id: str = "enwiki-20260701-v1"
    index_id: str = ""
    allow_incomplete_index: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 2
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 4.0
    disable_env_proxy: bool = True

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, snapshot_id: str = "") -> "WikiClientConfig":
        if not isinstance(value, dict):
            raise TypeError("wiki config must be a mapping.")
        base_url = str(value.get("base_url", "") or "").strip()
        if not base_url:
            raise ValueError("wiki.base_url is empty.")
        configured_snapshot = str(
            value.get("snapshot_id") or snapshot_id or "enwiki-20260701-v1"
        ).strip()
        return cls(
            base_url=base_url,
            snapshot_id=configured_snapshot,
            index_id=str(
                value.get("index_id") or canonical_index_id(configured_snapshot)
            ).strip(),
            allow_incomplete_index=bool(value.get("allow_incomplete_index", False)),
            timeout_seconds=max(
                1.0,
                float(value.get("timeout_seconds", value.get("timeout", 30.0))),
            ),
            max_retries=min(10, max(0, int(value.get("max_retries", 2)))),
            backoff_seconds=max(0.0, float(value.get("backoff_seconds", 0.5))),
            max_backoff_seconds=max(0.0, float(value.get("max_backoff_seconds", 4.0))),
            disable_env_proxy=bool(value.get("disable_env_proxy", True)),
        )


class WikiToolClient:
    RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        config: WikiClientConfig | dict[str, Any],
        *,
        snapshot_id: str = "",
        index_id: str = "",
        allow_incomplete_index: bool | None = None,
        transport: WikiTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = (
            config
            if isinstance(config, WikiClientConfig)
            else WikiClientConfig.from_mapping(config, snapshot_id=snapshot_id)
        )
        if index_id:
            self.config.index_id = str(index_id).strip()
        if not self.config.index_id:
            self.config.index_id = canonical_index_id(self.config.snapshot_id)
        if allow_incomplete_index is not None:
            self.config.allow_incomplete_index = bool(allow_incomplete_index)
        self._transport = transport
        self._sleep = sleep
        self._validated_manifest: dict[str, Any] | None = None

    def validate_index(self) -> dict[str, Any]:
        """Validate the immutable index identity before an Agent can use tools."""

        if self._validated_manifest is not None:
            return dict(self._validated_manifest)
        data = self._request_json("GET", "/manifest")
        response_snapshot = str(data.get("snapshot_id", "") or "").strip()
        response_index = str(data.get("index_id", "") or "").strip()
        manifest = data.get("manifest")
        if not isinstance(manifest, dict):
            raise WikiToolContractError("/manifest response is missing manifest object.")
        manifest_snapshot = str(manifest.get("snapshot_id", "") or "").strip()
        manifest_index = str(manifest.get("index_id", "") or "").strip()
        if (
            response_snapshot != self.config.snapshot_id
            or manifest_snapshot != self.config.snapshot_id
        ):
            raise WikiToolContractError(
                "/manifest snapshot mismatch: expected "
                f"{self.config.snapshot_id!r}, got response={response_snapshot!r}, "
                f"manifest={manifest_snapshot!r}."
            )
        if response_index != self.config.index_id or manifest_index != self.config.index_id:
            raise WikiToolContractError(
                "/manifest index mismatch: expected "
                f"{self.config.index_id!r}, got response={response_index!r}, "
                f"manifest={manifest_index!r}."
            )
        response_status = str(data.get("build_status", "") or "").strip().lower()
        manifest_status = str(manifest.get("status", "") or "").strip().lower()
        if not response_status or response_status != manifest_status:
            raise WikiToolContractError(
                "/manifest build status is missing or inconsistent: "
                f"response={response_status!r}, manifest={manifest_status!r}."
            )
        build_status = response_status
        source = manifest.get("source")
        manifest_complete = (
            source.get("complete_snapshot") if isinstance(source, dict) else None
        )
        response_complete = data.get("complete_snapshot")
        if not isinstance(response_complete, bool) or not isinstance(
            manifest_complete, bool
        ) or response_complete is not manifest_complete:
            raise WikiToolContractError(
                "/manifest complete_snapshot is missing or inconsistent: "
                f"response={response_complete!r}, manifest={manifest_complete!r}."
            )
        complete_snapshot = response_complete
        index_config = manifest.get("index")
        if not isinstance(index_config, dict):
            raise WikiToolContractError(
                "/manifest index is missing behavior-bearing build config."
            )
        observed_build = {
            "backend": index_config.get("backend"),
            "namespace": index_config.get("namespace"),
            "chunk_chars": index_config.get("chunk_chars"),
            "chunk_overlap": index_config.get("chunk_overlap"),
            "cleaner": index_config.get("cleaner"),
        }
        if observed_build["backend"] != INDEX_BACKEND:
            raise WikiToolContractError(
                "/manifest index backend mismatch: "
                f"expected {INDEX_BACKEND!r}, got {observed_build['backend']!r}."
            )
        try:
            behavior_bound_id = index_id_for_build(
                self.config.snapshot_id,
                namespace=int(observed_build["namespace"]),
                chunk_chars=int(observed_build["chunk_chars"]),
                chunk_overlap=int(observed_build["chunk_overlap"]),
                cleaner=str(observed_build["cleaner"] or ""),
            )
            behavior_fingerprint = index_build_fingerprint(
                namespace=int(observed_build["namespace"]),
                chunk_chars=int(observed_build["chunk_chars"]),
                chunk_overlap=int(observed_build["chunk_overlap"]),
                cleaner=str(observed_build["cleaner"] or ""),
            )
        except (TypeError, ValueError) as exc:
            raise WikiToolContractError(
                f"/manifest index build config is invalid: {observed_build!r}."
            ) from exc
        if behavior_bound_id != response_index:
            raise WikiToolContractError(
                "/manifest index_id is not bound to its build config: "
                f"expected {behavior_bound_id!r}, got {response_index!r}."
            )
        if self.config.index_id != behavior_bound_id:
            raise WikiToolContractError(
                "Configured Wikipedia index_id does not match the manifest build "
                f"config: expected {behavior_bound_id!r}, got "
                f"{self.config.index_id!r}."
            )
        manifest_fingerprint = index_config.get("build_fingerprint")
        if manifest_fingerprint not in (None, behavior_fingerprint):
            raise WikiToolContractError(
                "/manifest build fingerprint does not match its index build config."
            )
        if complete_snapshot and build_status == "complete":
            canonical_build = {
                "backend": INDEX_BACKEND,
                "namespace": CANONICAL_INDEX_NAMESPACE,
                "chunk_chars": CANONICAL_INDEX_CHUNK_CHARS,
                "chunk_overlap": CANONICAL_INDEX_CHUNK_OVERLAP,
                "cleaner": CANONICAL_INDEX_CLEANER,
            }
            if observed_build != canonical_build:
                raise WikiToolContractError(
                    "/manifest complete index build config is not canonical: "
                    f"observed={observed_build!r}, expected={canonical_build!r}."
                )
        if build_status != "complete" or not complete_snapshot:
            if build_status != "partial":
                raise WikiToolContractError(
                    "Wikipedia incomplete index is not a static partial artifact: "
                    f"build_status={build_status!r}."
                )
            if not self.config.allow_incomplete_index:
                raise WikiToolContractError(
                    "Wikipedia index is not a complete snapshot: "
                    f"build_status={build_status!r}, "
                    f"complete_snapshot={complete_snapshot!r}. Pilot indexes require "
                    "the explicit allow_incomplete_index option."
                )
        validated = {
            "snapshot_id": response_snapshot,
            "index_id": response_index,
            "build_status": build_status,
            "complete_snapshot": complete_snapshot,
            "allow_incomplete_index": self.config.allow_incomplete_index,
        }
        self._validated_manifest = validated
        return dict(validated)

    def search(self, query: str, top_k: int = 8) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise WikiToolContractError("search.query must be non-empty.")
        _bounded_text(
            query, name="search.query", maximum=MAX_SEARCH_QUERY_CHARS
        )
        top_k = _bounded_int(top_k, name="search.top_k", minimum=1, maximum=20)
        return self._request("/v1/search", {"query": query, "top_k": top_k})

    def open(
        self,
        source_id: str,
        cursor: int = 0,
        max_chars: int = 6000,
    ) -> dict[str, Any]:
        source_id = str(source_id or "").strip()
        if not source_id:
            raise WikiToolContractError("open.source_id must be non-empty.")
        _bounded_text(
            source_id, name="open.source_id", maximum=MAX_SOURCE_ID_CHARS
        )
        cursor = _bounded_int(cursor, name="open.cursor", minimum=0, maximum=2**63 - 1)
        max_chars = _bounded_int(
            max_chars, name="open.max_chars", minimum=128, maximum=MAX_OPEN_CHARS
        )
        return self._request(
            "/v1/open",
            {"source_id": source_id, "cursor": cursor, "max_chars": max_chars},
        )

    def find(
        self,
        source_id: str,
        pattern: str,
        max_results: int = 10,
    ) -> dict[str, Any]:
        source_id = str(source_id or "").strip()
        pattern = str(pattern or "").strip()
        if not source_id:
            raise WikiToolContractError("find.source_id must be non-empty.")
        if not pattern:
            raise WikiToolContractError("find.pattern must be non-empty.")
        _bounded_text(
            source_id, name="find.source_id", maximum=MAX_SOURCE_ID_CHARS
        )
        _bounded_text(
            pattern, name="find.pattern", maximum=MAX_FIND_PATTERN_CHARS
        )
        max_results = _bounded_int(
            max_results, name="find.max_results", minimum=1, maximum=50
        )
        return self._request(
            "/v1/find",
            {
                "source_id": source_id,
                "pattern": pattern,
                "max_results": max_results,
            },
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise WikiToolContractError("Tool arguments must be an object.")
        unknown = set(arguments) - {
            "search": {"query", "top_k"},
            "open": {"source_id", "cursor", "max_chars"},
            "find": {"source_id", "pattern", "max_results"},
        }.get(name, set())
        if name not in {"search", "open", "find"}:
            raise WikiToolContractError(f"Unknown Wikipedia tool: {name!r}.")
        if unknown:
            raise WikiToolContractError(
                f"Unexpected {name} argument(s): {sorted(unknown)}."
            )
        if name == "search":
            return self.search(arguments.get("query", ""), arguments.get("top_k", 8))
        if name == "open":
            return self.open(
                arguments.get("source_id", ""),
                arguments.get("cursor", 0),
                arguments.get("max_chars", 6000),
            )
        return self.find(
            arguments.get("source_id", ""),
            arguments.get("pattern", ""),
            arguments.get("max_results", 10),
        )

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", path, payload)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}{path}"
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                data = self._send(method, url, payload or {})
                if path != "/manifest":
                    self._validate_response(data, path)
                return data
            except _RetryableWikiError as exc:
                if attempt >= attempts:
                    raise WikiToolError(
                        f"{path} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                delay = min(
                    self.config.max_backoff_seconds,
                    self.config.backoff_seconds * (2 ** (attempt - 1)),
                )
                if delay:
                    self._sleep(delay)
        raise WikiToolError(f"{path} failed without a response.")  # pragma: no cover

    def _send(
        self, method: str, url: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._transport is not None:
            data = self._transport(url, payload, self.config.timeout_seconds)
            if not isinstance(data, dict):
                raise _RetryableWikiError("Injected wiki transport returned non-object JSON.")
            return data
        session = requests.Session()
        session.trust_env = not self.config.disable_env_proxy
        try:
            if method == "GET":
                response = session.get(url, timeout=self.config.timeout_seconds)
            else:
                response = session.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.config.timeout_seconds,
                )
        except requests.RequestException as exc:
            raise _RetryableWikiError(str(exc)) from exc
        finally:
            session.close()
        if response.status_code in self.RETRYABLE_STATUS_CODES:
            raise _RetryableWikiError(
                f"HTTP {response.status_code}: {response.text[:1000]}"
            )
        if response.status_code >= 400:
            raise WikiToolError(
                f"HTTP {response.status_code}: {response.text[:2000]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise _RetryableWikiError("Wiki service returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise _RetryableWikiError("Wiki service returned non-object JSON.")
        return data

    def _validate_response(self, data: dict[str, Any], path: str) -> None:
        response_snapshot = str(data.get("snapshot_id", "") or "")
        if not response_snapshot:
            raise WikiToolContractError(f"{path} response is missing snapshot_id.")
        if response_snapshot != self.config.snapshot_id:
            raise WikiToolContractError(
                f"{path} snapshot mismatch: expected {self.config.snapshot_id!r}, "
                f"got {response_snapshot!r}."
            )
        required_key = {
            "/v1/search": "results",
            "/v1/open": "content",
            "/v1/find": "matches",
        }[path]
        if required_key not in data:
            raise WikiToolContractError(
                f"{path} response is missing required field {required_key!r}."
            )


class _RetryableWikiError(WikiToolError):
    pass


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise WikiToolContractError(f"{name} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WikiToolContractError(f"{name} must be an integer.") from exc
    if parsed < minimum or parsed > maximum:
        raise WikiToolContractError(
            f"{name} must be between {minimum} and {maximum}; got {parsed}."
        )
    return parsed


def _bounded_text(value: str, *, name: str, maximum: int) -> str:
    if len(value) > maximum:
        raise WikiToolContractError(
            f"{name} must contain at most {maximum} characters."
        )
    return value


def extract_source_ids(value: Any) -> list[str]:
    """Collect stable source IDs in first-seen order from a tool response."""

    found: list[str] = []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            source_id = node.get("source_id")
            if isinstance(source_id, str) and source_id and source_id not in seen:
                seen.add(source_id)
                found.append(source_id)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found
