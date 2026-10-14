# Metrics

The [container guide](../../../docker/README.md) provides network-disabled metrics
commands using the built-in benchmark, with JSON and system/case CSV exports.

`coverage.py` implements the bottom-up topology-aware coverage metric in the
paper. `topology.py` validates parent rubric references and converts the case tree
to the recursive metric; `aggregate.py` computes case scores and separate macro
means per system.

```bash
drbenchpro metrics \
  --cases examples/evaluator_case.json \
  --scores examples/rubric_scores.jsonl \
  --output outputs/metrics.json
```

## Score mapping

The Judge output is ternary: `1` means Satisfied, `0.5` means Partially
Satisfied, and `0` means Not Satisfied. Metric computation is binary, following
the paper: only `1` is covered; both `0.5` and `0` are mapped to uncovered.
The original score rows are not modified.

## Recursive metric

All outputs lie in `[0,1]`. Multiply by 100 only for percentage display. The
fixed parameters are `beta=0.60`, `alpha=0.25`, `eta=0.75`, and `rho=0.50`.

- Every actual leaf, including a shallower leaf, is initialized as
  `B(v) = D(v) = x(v)`.
- At a non-leaf node, breadth is
  `B(v) = beta*x(v) + (1-beta)*mean(B(children))`.
- Depth first sorts child Depth values in descending order and applies normalized
  rank weights `rho**rank`. It then computes
  `D(v) = alpha*x(v) + (1-alpha)*d(v)*(eta+(1-eta)*x(v))`.
- Case breadth is the mean across first-hop research directions. Case depth uses
  the same rank aggregation across those directions.
- Case overall coverage is `sqrt(Breadth * Depth)`.
- System Breadth, Depth, and Overall are separate means of their case values.
  System Overall is **not** `sqrt(mean(Breadth)*mean(Depth))`.

Metric outputs identify this contract as `drbenchpro-coverage-v1` and record the
parameters and ternary-to-binary mapping alongside the scores.

Missing rubric scores are rejected. By default, every system must cover all input
cases. `--allow-subset` permits an explicitly partial case set and records its
actual and expected counts. No missing case is silently assigned zero or removed
from an allegedly complete benchmark.

## Result display and CSV

```bash
drbenchpro metrics --cases data/benchmark/cases.jsonl --scores outputs/eval/scores.jsonl \
  --output outputs/eval/metrics.json --csv outputs/eval/systems.csv --case-csv outputs/eval/cases.csv
```

Use `--allow-subset` only for explicitly partial runs. A terminal table on stderr
shows system name, evaluated/expected case counts, COMPLETE/PARTIAL and all three
metrics. Stdout remains JSON for scripts; full-precision JSON is the canonical
result. System CSV columns are `system_id,case_count,expected_case_count,partial,
breadth,depth,overall`; case CSV has `system_id,case_id,breadth,depth,overall`.
CSV uses the same 0–1 values, without percentage conversion or recomputation.
Multiple systems are exported as separate rows, retaining partial coverage markers.
CSV/JSON output paths must be distinct and must not overwrite the input files.
