"""Reference implementation of the DRBenchPro coverage metric.

The metric follows the bottom-up recursion in the paper. Judge scores remain
ternary at the evaluation boundary, but metric computation treats only a fully
satisfied rubric as covered: ``1 -> 1`` and ``0.5/0 -> 0``.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from typing import TypeVar


METRIC_VERSION = "drbenchpro-coverage-v1"
BETA = 0.60
ALPHA = 0.25
ETA = 0.75
RHO = 0.50

NodeId = TypeVar("NodeId", bound=Hashable)


def _check_unit_interval(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must lie in [0, 1], got {value}")


def binary_coverage_score(score: float) -> float:
    """Map a ternary judge score to the binary coverage value used by metrics."""
    if isinstance(score, bool) or score not in (0, 0.5, 1):
        raise ValueError(f"judge score must be 0, 0.5 or 1, got {score!r}")
    return 1.0 if score == 1 else 0.0


def rank_aggregate(values: Sequence[float], rho: float = RHO) -> float:
    """Aggregate descending values with normalized geometric rank weights."""
    if not 0.0 < rho < 1.0:
        raise ValueError(f"rho must lie in (0, 1), got {rho}")
    for value in values:
        _check_unit_interval("rank-aggregation value", value)
    if not values:
        return 0.0
    ordered = sorted(values, reverse=True)
    weights = [rho**index for index in range(len(ordered))]
    return sum(weight * value for weight, value in zip(weights, ordered)) / sum(weights)


def breadth_update(
    x: float,
    child_breadth: Sequence[float],
    beta: float = BETA,
) -> float:
    """Return breadth at one node, using leaf initialization when appropriate."""
    _check_unit_interval("x", x)
    if not 0.0 < beta < 1.0:
        raise ValueError(f"beta must lie in (0, 1), got {beta}")
    for value in child_breadth:
        _check_unit_interval("child breadth", value)
    if not child_breadth:
        return x
    return beta * x + (1.0 - beta) * sum(child_breadth) / len(child_breadth)


def depth_update(
    x: float,
    child_depth: Sequence[float],
    *,
    alpha: float = ALPHA,
    eta: float = ETA,
    rho: float = RHO,
) -> float:
    """Return depth at one node, using leaf initialization when appropriate."""
    _check_unit_interval("x", x)
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
    if not 0.0 < eta < 1.0:
        raise ValueError(f"eta must lie in (0, 1), got {eta}")
    for value in child_depth:
        _check_unit_interval("child depth", value)
    if not child_depth:
        return x
    descendant_depth = rank_aggregate(child_depth, rho)
    return alpha * x + (1.0 - alpha) * descendant_depth * (
        eta + (1.0 - eta) * x
    )


def case_metrics(
    roots: Sequence[NodeId],
    children: Mapping[NodeId, Sequence[NodeId]],
    judge_scores: Mapping[NodeId, float],
    *,
    beta: float = BETA,
    alpha: float = ALPHA,
    eta: float = ETA,
    rho: float = RHO,
) -> tuple[float, float, float]:
    """Compute case breadth, depth and overall coverage over a rooted forest.

    ``roots`` are the children of the unscored case root. Every supplied node is
    an incoming relation/rubric and must be reachable exactly once from a root.
    """
    if not roots:
        raise ValueError("at least one research direction is required")
    if len(set(roots)) != len(roots):
        raise ValueError("research direction IDs must be unique")

    scores = {
        node_id: binary_coverage_score(score)
        for node_id, score in judge_scores.items()
    }
    visiting: set[NodeId] = set()
    visited: set[NodeId] = set()

    def evaluate(node_id: NodeId) -> tuple[float, float]:
        if node_id not in scores:
            raise ValueError(f"missing judge score for node {node_id!r}")
        if node_id in visiting:
            raise ValueError("coverage topology contains a cycle")
        if node_id in visited:
            raise ValueError(f"coverage topology reuses node {node_id!r}")
        visiting.add(node_id)
        child_results = [evaluate(child_id) for child_id in children.get(node_id, ())]
        visiting.remove(node_id)
        visited.add(node_id)
        x = scores[node_id]
        breadth = breadth_update(x, [result[0] for result in child_results], beta)
        depth = depth_update(
            x,
            [result[1] for result in child_results],
            alpha=alpha,
            eta=eta,
            rho=rho,
        )
        return breadth, depth

    branch_results = [evaluate(root_id) for root_id in roots]
    if visited != scores.keys():
        unreachable = sorted((repr(node_id) for node_id in scores.keys() - visited))
        raise ValueError(f"unreachable scored nodes: {', '.join(unreachable[:5])}")

    breadth = sum(result[0] for result in branch_results) / len(branch_results)
    depth = rank_aggregate([result[1] for result in branch_results], rho)
    overall = math.sqrt(breadth * depth)
    if not all(0.0 <= value <= 1.0 for value in (breadth, depth, overall)):
        raise AssertionError("coverage metric outputs must lie in [0, 1]")
    return breadth, depth, overall


def system_metrics(
    cases: Sequence[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """Macro-average case B, D and S; never recompute S from mean B and D."""
    if not cases:
        raise ValueError("at least one case is required")
    count = len(cases)
    return tuple(sum(case[index] for case in cases) / count for index in range(3))


def self_test() -> None:
    assert binary_coverage_score(1) == 1.0
    assert binary_coverage_score(0.5) == 0.0
    assert binary_coverage_score(0) == 0.0
    assert rank_aggregate([1.0, 0.0]) == 2.0 / 3.0
    assert breadth_update(1.0, []) == 1.0
    assert depth_update(1.0, []) == 1.0
    assert breadth_update(1.0, [0.0]) == BETA
    assert depth_update(0.0, [1.0]) == (1.0 - ALPHA) * ETA
    assert case_metrics(["root"], {"root": []}, {"root": 1}) == (1.0, 1.0, 1.0)
    assert case_metrics(["root"], {"root": []}, {"root": 0.5}) == (0.0, 0.0, 0.0)


if __name__ == "__main__":
    self_test()
    print(
        f"{METRIC_VERSION} reference self-test passed: "
        f"beta={BETA}, alpha={ALPHA}, eta={ETA}, rho={RHO}"
    )
