"""Identifier helpers for graph and benchmark artifacts."""

from __future__ import annotations

import hashlib
import re


def _hash_text(text: str, length: int = 12) -> str:
    """Return a short, stable hash for a text input."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def slugify(text: str) -> str:
    """Convert a free-form string into a filesystem and id safe slug."""
    cleaned = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "_", cleaned)


def _ordered_width(total: int | None, minimum: int) -> int:
    """Choose a readable zero-pad width for ordered artifact ids."""
    if total is None:
        return minimum
    try:
        parsed = max(1, int(total))
    except Exception:
        return minimum
    return max(minimum, len(str(parsed)))


def make_graph_id(seed_entity: str) -> str:
    """Create a stable graph id from the seed entity."""
    return f"graph_{slugify(seed_entity)}_{_hash_text(seed_entity)}"


def make_entity_id(wikipedia_title: str) -> str:
    """Create a stable entity node id from title."""
    return f"ent_{slugify(wikipedia_title)}_{_hash_text(wikipedia_title)}"


def make_anchor_id(
    source_title: str,
    target_title: str,
    paragraph_id: int,
    start: int,
    end: int,
) -> str:
    """Create a stable anchor id from local evidence coordinates."""
    payload = f"{source_title}|{target_title}|{paragraph_id}|{start}|{end}"
    return f"anc_{_hash_text(payload, length=16)}"


def make_edge_id(source_title: str, target_title: str, anchor_id: str) -> str:
    """Create a stable edge id bound to source, target and anchor."""
    payload = f"{source_title}|{target_title}|{anchor_id}"
    return f"edge_{_hash_text(payload, length=16)}"


def make_ordered_edge_id(index: int, total: int | None = None) -> str:
    """Create a readable ordered edge id."""
    width = _ordered_width(total, minimum=3)
    return f"edge_{int(index):0{width}d}"


def make_ordered_subgraph_id(index: int, total: int | None = None) -> str:
    """Create a readable ordered subgraph id."""
    width = _ordered_width(total, minimum=2)
    return f"subgraph_{int(index):0{width}d}"


def make_ordered_rubric_id(subgraph_id: str, edge_id: str) -> str:
    """Create a readable rubric id that remains unique across subgraphs."""
    return f"rubric_{str(subgraph_id).strip()}_{str(edge_id).strip()}"


def make_ordered_rubric_input_id(subgraph_id: str, edge_id: str) -> str:
    """Create a readable rubric-input id aligned with the rubric id."""
    return f"rubric_input_{str(subgraph_id).strip()}_{str(edge_id).strip()}"


def make_sample_id(graph_id: str) -> str:
    """Create a benchmark sample id from graph id."""
    return f"sample_{_hash_text(graph_id, length=16)}"
