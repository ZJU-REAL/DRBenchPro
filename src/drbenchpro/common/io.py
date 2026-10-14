"""IO utilities for config and JSON artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def ensure_parent_dir(path: str | Path) -> None:
    """Create parent directory for a file path if needed."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load YAML config from disk."""
    with Path(path).open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"YAML root must be dict, got {type(loaded).__name__}")
    return loaded


def load_json(path: str | Path) -> dict[str, Any]:
    """Load JSON object from disk."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be object, got {type(data).__name__}")
    return data


def dump_json(path: str | Path, data: dict[str, Any]) -> None:
    """Write JSON object to disk with UTF-8 and indentation."""
    ensure_parent_dir(path)
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def deep_update(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge two dictionaries, mutating `base`."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
