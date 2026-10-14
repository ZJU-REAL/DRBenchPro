# External reports and offline examples

These are synthetic examples, not the paper's model results. `example-001` exists
in `evaluator_case.json` only; do not use it with the full benchmark case file.

`reports.jsonl` demonstrates the public report interface:

```json
{"case_id":"example-001","system_id":"my-research-system","report":"The fictional archive stores research notes organized by topic.\n\nThe topics have an index."}
```

- `case_id`: exact evaluator case ID; benchmark IDs come from `data/benchmark/queries.jsonl`.
- `system_id`: your system name; each system/case pair must occur once. Use distinct
  names for different backbones or experimental configurations. If omitted, it is `default`.
- `report`: the complete visible report as a nonempty string. A Python JSON writer
  escapes Markdown newlines automatically; each JSON object occupies one physical line.

Extra fields are not sent as report content to Judge. An optional `status` of
`truncated` is reported by validation but does not prevent grading the visible text.
Independent systems may supply their own reports; our generation service is not required.

```bash
drbenchpro validate-reports --cases examples/evaluator_case.json --reports examples/reports.jsonl --require-full
# Set JUDGE_API_KEY, JUDGE_BASE_URL, JUDGE_MODEL first; the next command makes model calls:
drbenchpro evaluate --cases examples/evaluator_case.json --reports examples/reports.jsonl --output outputs/external-example
# No model calls for metrics:
drbenchpro metrics --cases examples/evaluator_case.json --scores outputs/external-example/scores.jsonl \
  --output outputs/external-example/metrics.json --csv outputs/external-example/systems.csv
```

To write your full-benchmark reports safely in Python:

```python
import json
rows = [{"case_id": case_id, "system_id": "my-system", "report": generated_report}]
with open("my-reports.jsonl", "w", encoding="utf-8") as stream:
    for row in rows:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
```

Substitute your actual case ID and generated report. Use the frozen evaluator
`data/benchmark/cases.jsonl` for validation/Judge. Judge may evaluate a subset;
full benchmark metrics require all cases per system. Missing reports or exhausted
Judge calls are not equivalent to zero scores and must not be silently omitted
from a result presented as complete.

For a model-free demonstration use `rubric_scores.jsonl` with `evaluator_case.json`.
