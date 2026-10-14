"""Future hook for turning structured query specs into natural-language queries."""

from __future__ import annotations

from typing import Protocol

from drbenchpro.construction.subgraphs.schemas import QuerySpec


class QueryTextGenerator(Protocol):
    """Protocol for future LLM-backed query generation components."""

    def generate(self, query_spec: QuerySpec) -> str:
        """Return a natural-language query for one structured query spec."""


def generate_query_text_from_spec(
    query_spec: QuerySpec,
    generator: QueryTextGenerator | None = None,
) -> str:
    """
    Convert a structured query spec into a natural-language query.

    V1 keeps this as a pure-programmatic stub so later LLM integration can plug
    into a stable interface without changing the subgraph sampling pipeline.
    """
    if generator is not None:
        return generator.generate(query_spec)

    focus = ", ".join(query_spec.focus_nodes[:4]) if query_spec.focus_nodes else query_spec.anchor_topic
    intent = query_spec.intended_user_intent or "structured analysis"
    return (
        "TODO: generate natural-language query from structured query spec. "
        f"Anchor '{query_spec.anchor_topic}', type '{query_spec.subgraph_type}', "
        f"intent '{intent}', focus '{focus}'."
    )
