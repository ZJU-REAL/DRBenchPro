"""Reject ambiguous IDs, malformed trees and invalid score scales."""
import math

def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")

def validate_case(case):
    text(case.get("case_id"), "case_id")
    text(case.get("query"), "query")
    rubrics = case.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        raise ValueError("rubrics must be a nonempty list")
    by_id = {}
    for r in rubrics:
        for name in ("rubric_id", "rubric_text"):
            text(r.get(name), name)
        if r["rubric_id"] in by_id:
            raise ValueError("Duplicate rubric ID")
        by_id[r["rubric_id"]] = r
        if type(r.get("rubric_depth")) is not int or r["rubric_depth"] not in (1, 2, 3):
            raise ValueError("rubric_depth must be 1, 2 or 3")
        if "parent_rubric_id" not in r:
            raise ValueError("Missing parent_rubric_id")
        parent = r["parent_rubric_id"]
        if r["rubric_depth"] == 1:
            if parent is not None:
                raise ValueError("Depth-1 rubric requires a null parent_rubric_id")
        else:
            text(parent, "parent_rubric_id")
    if not any(r["rubric_depth"] == 1 for r in rubrics):
        raise ValueError("Missing depth-1 branch")
    for r in rubrics:
        parent = r["parent_rubric_id"]
        if parent is not None:
            if parent not in by_id or by_id[parent]["rubric_depth"] != r["rubric_depth"] - 1:
                raise ValueError("Missing parent or inconsistent depth")
    return case

def validate_scores(case, rows):
    expected = {r["rubric_id"] for r in case["rubrics"]}
    found = {}
    for row in rows:
        if row.get("case_id") != case["case_id"]:
            raise ValueError("Score case_id mismatch")
        rid, score = row.get("rubric_id"), row.get("score")
        if rid in found or rid not in expected:
            raise ValueError("Duplicate or unknown rubric score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score not in (0, .5, 1):
            raise ValueError("Score must be 0, 0.5 or 1")
        found[rid] = float(score)
    if found.keys() != expected:
        raise ValueError(f"Missing {len(expected - found.keys())} rubric scores")
    return found
