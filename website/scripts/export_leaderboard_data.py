"""Export validated domain means for the static project leaderboard (stdlib only)."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import mean


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain_csv", type=Path)
    args = parser.parse_args()
    website = Path(__file__).resolve().parents[1]
    global_data = json.loads((website / "assets/data/results.json").read_text())
    systems = {row["system_id"]: row for row in global_data["results"]}
    with args.domain_csv.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    domains = {}
    seen = set()
    metrics = {"S": "overall_percent", "B": "breadth_percent", "D": "depth_percent"}
    for row in rows:
        system_id, domain = row["system_id"], row["domain"]
        key = (system_id, domain)
        if system_id not in systems or key in seen or int(row["case_count"]) != 6:
            raise ValueError(f"Unexpected, duplicate, or incomplete system–domain cell: {key}")
        if row["baseline_id"] != "1.1-beta0.60":
            raise ValueError("Domain results must match the published metric baseline")
        seen.add(key)
        values = {field: float(row[column]) for column, field in metrics.items()}
        if not all(math.isfinite(value) and 0 <= value <= 100 for value in values.values()):
            raise ValueError(f"Invalid percentage: {key}")
        bucket = domains.setdefault(domain, {"name": domain, "case_count": 6, "results": []})
        bucket["results"].append({"system_id": system_id, **values})
    if len(domains) != 24 or len(seen) != 384:
        raise ValueError("Expected 24 complete domains and 384 system–domain cells")
    for domain in domains.values():
        if {row["system_id"] for row in domain["results"]} != set(systems):
            raise ValueError(f"Incomplete domain: {domain['name']}")
    max_error = 0
    for system_id, system in systems.items():
        cells = [row for domain in domains.values() for row in domain["results"] if row["system_id"] == system_id]
        for field in metrics.values():
            error = abs(mean(row[field] for row in cells) - system[field])
            max_error = max(max_error, error)
            if error > 1e-9:
                raise ValueError(f"Domain means disagree with global results: {system_id}/{field}")
    output = {
        "snapshot": global_data["snapshot"],
        "baseline_id": "1.1-beta0.60",
        "aggregation": "Mean of case-level scores over six cases per system–domain cell; percentages",
        "provenance": {
            "source": "analysis/domain_coverage_heatmaps/domain_scheme3_3_final_v3_metrics.csv",
            "source_sha256": hashlib.sha256(args.domain_csv.read_bytes()).hexdigest(),
            "global_validation_max_abs_error": max_error,
        },
        "domains": [domains[name] for name in sorted(domains)],
    }
    target = website / "assets/data/domain-results.json"
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(f"Exported {len(seen)} cells / {len(domains)} domains; maximum global error {max_error:.3g}")


if __name__ == "__main__":
    main()
