"""Rule-based research query generation from local graph evidence."""

from __future__ import annotations

from typing import Any


def generate_query_rule(graph: dict[str, Any]) -> dict[str, Any]:
    """
    Generate a research-style task from graph metadata and edge coverage.

    This query is intentionally not factoid QA. It asks for structured synthesis
    across multiple linked topics grounded in the graph.
    """
    metadata = graph.get("metadata", {})
    seed = metadata.get("seed_entity", "the seed entity")

    coverage_hint = (
        "several major unnamed research angles surfaced by the sampled graph, including methodological, "
        "historical, institutional, application, evaluation, or impact-oriented connections where relevant"
    )

    query = (
        f"Prepare a deep research brief centered on '{seed}'. Synthesize how this entity is connected to "
        f"{coverage_hint}. The report should integrate multiple evidence-backed relationships, compare the roles "
        f"of different unnamed aspects, and explain why each connection matters to understanding '{seed}'. "
        "Do not answer as a single factoid; provide a structured analysis backed by explicit graph evidence."
    )
    return {
        "query": query,
        "query_type": "research_brief",
        "research_goal": f"Explain how the seed entity is connected to the central linked themes surfaced in the sampled graph around '{seed}'.",
        "expected_coverage": [
            "multiple unnamed first-hop research angles",
            "supporting downstream context where it is relevant",
            "why the main connections matter for understanding the seed entity",
        ],
        "why_not_answerable_from_memory": "A strong answer should connect multiple linked graph-grounded topics rather than recalling one isolated fact.",
        "subgraph_binding": {
            "seed_entity": seed,
            "central_nodes": [],
            "central_relation_families": [],
            "excluded_or_deemphasized_regions": [],
        },
    }
