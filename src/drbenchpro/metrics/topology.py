"""Map a validated rubric tree into the recursive coverage metric."""

from drbenchpro.data.validation import validate_case, validate_scores

from .coverage import case_metrics


def score_case(case, rows):
    validate_case(case)
    scores_by_rubric_id = validate_scores(case, rows)
    rubrics = case["rubrics"]
    children = {rubric["rubric_id"]: [] for rubric in rubrics}
    for rubric in rubrics:
        parent_rubric_id = rubric["parent_rubric_id"]
        if parent_rubric_id is not None:
            children[parent_rubric_id].append(rubric["rubric_id"])
    roots = [
        rubric["rubric_id"]
        for rubric in rubrics
        if rubric["parent_rubric_id"] is None
    ]
    breadth, depth, overall = case_metrics(roots, children, scores_by_rubric_id)
    return {
        "case_id": case["case_id"],
        "breadth": breadth,
        "depth": depth,
        "overall": overall,
    }
