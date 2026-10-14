from collections import defaultdict
from .topology import score_case
from .coverage import ALPHA, BETA, ETA, METRIC_VERSION, RHO, system_metrics

def aggregate(cases, scores, allow_subset=False):
    grouped = defaultdict(lambda: defaultdict(list))
    for row in scores:
        cid = row.get("case_id")
        if cid not in cases:
            raise ValueError(f"Unknown case: {cid}")
        sid = row.get("system_id", "default")
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError("Invalid system_id")
        grouped[sid][cid].append(row)
    if not grouped:
        raise ValueError("No scores")
    result, systems = [], []
    for sid, by_case in sorted(grouped.items()):
        if not allow_subset and by_case.keys() != cases.keys():
            raise ValueError("Incomplete case coverage; use --allow-subset for an explicitly partial run")
        rows = [{"system_id": sid, **score_case(cases[cid], rs)} for cid, rs in sorted(by_case.items())]
        result.extend(rows)
        b, d, s = system_metrics([(r["breadth"], r["depth"], r["overall"]) for r in rows])
        systems.append({"system_id": sid, "case_count": len(rows), "expected_case_count": len(cases), "partial": len(rows) != len(cases), "breadth": b, "depth": d, "overall": s})
    return {
        "metric_version": METRIC_VERSION,
        "scale": "0-1",
        "judge_score_mapping": {"1": 1, "0.5": 0, "0": 0},
        "parameters": {"beta": BETA, "alpha": ALPHA, "eta": ETA, "rho": RHO},
        "cases": result,
        "systems": systems,
    }
