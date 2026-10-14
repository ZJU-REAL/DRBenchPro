# Evaluation

Evaluate model-generated reports against the benchmark rubrics, then compute
Breadth, Depth and Overall with `drbenchpro metrics`. Each rubric receives a
score of `1` (satisfied), `0.5` (partially satisfied), or `0` (not satisfied).

The two English Judge templates are:

| Mode | Template | Requested fields per rubric | Usage |
| --- | --- | --- | --- |
| `score_only_half` | [report_scoring_final_score_only-half.md](../resources/prompts/evaluation/report_scoring_final_score_only-half.md) | `rubric_id`, `score` | Formal benchmark CLI; Python API default |
| `score_reason_half` | [report_scoring_final_score_reason-half.md](../resources/prompts/evaluation/report_scoring_final_score_reason-half.md) | `rubric_id`, `score`, `reason` | Optional Python API mode |

`drbenchpro evaluate` uses `score_only_half` and does not request reasons.
Use the Python API below when explanations are needed.

The [standalone container guide](../../../docker/README.md) covers external-agent
report validation, independent Judge env files, preflight, persistent output and
resume. Container evaluation uses the built-in cases and does not require Wiki
or the Research API.

## Run the benchmark

Run these commands from the repository root after installation. Set
`JUDGE_API_KEY`, `JUDGE_BASE_URL` and `JUDGE_MODEL` in your shell. The URL is the
compatible API base, normally ending in `/v1`; use a model name accepted by your
endpoint. Keep the Judge model and configuration consistent when comparing systems.

Generate reports from `data/benchmark/queries.jsonl` using your own model, and
save them as `my-reports.jsonl` following the input format below. The commands
use the installed default evaluation configuration:

```bash
drbenchpro validate-reports --cases data/benchmark/cases.jsonl \
  --reports my-reports.jsonl --require-full
drbenchpro evaluate --cases data/benchmark/cases.jsonl \
  --reports my-reports.jsonl --output outputs/my-evaluation
# Reuse completed reports with the same inputs and configuration:
drbenchpro evaluate --cases data/benchmark/cases.jsonl \
  --reports my-reports.jsonl --output outputs/my-evaluation --resume
drbenchpro metrics --cases data/benchmark/cases.jsonl \
  --scores outputs/my-evaluation/scores.jsonl --output outputs/my-metrics.json
```

For editable configuration, run `drbenchpro init-config --output configs` and
pass `--config configs/evaluation.yaml` to `evaluate`. Keep `output_format` set
to `score_only_half` for the CLI.

## Configuration, outputs and recovery

The default config sends the full report and all case rubrics in one batch
(`chunk_size: 0`), then retries failed batches at smaller sizes
`160,110,80,40,20,10,5,2,1`. The model client uses `max_retries: 10`, meaning
**10 total transport attempts** in this client. Each fallback chunk
gets its own transport-attempt allowance. Temperature is omitted, timeout is
1200 seconds, cache is disabled, and provider-specific cache hints default off.

Outputs:

- `scores.jsonl`: only fully validated scores for completed reports.
- `checkpoint.json`: scores, completed case/system pairs, errors, and input identity.
- `run.json`: model/config metadata without API keys, prompt hash and coverage.

An exhausted call, malformed JSON, missing/duplicate/unknown rubric ID or invalid
score fails that report. Errors are not converted to zero scores. The command
returns nonzero if any report failed. Resume is at completed-report granularity;
a partially failed report is retried. The identity includes the reports, case
material, resolved model/config and composed prompt; changing them requires a new
output directory. Credential rotation alone does not change this identity.

Use `drbenchpro metrics` on `scores.jsonl` to compute the actual paper metrics.
The Python judge API's `overall_score` is the mean rubric score. Structural
Overall combines Breadth and Depth and is computed by `drbenchpro metrics`.

The public half-credit parser rejects malformed, missing or non-ternary raw
scores before normalization.

## Reason-output Python API

Select `output_format="score_reason_half"` to request an explanation alongside
each rubric score. For example, evaluate the supplied example report using
the same `JUDGE_*` shell variables as the CLI:

```python
import json
from pathlib import Path

from drbenchpro.evaluation.judge import evaluate_rubrics_final_llm
from drbenchpro.llm.config import load_config, resolve_model

case = json.loads(Path("examples/evaluator_case.json").read_text(encoding="utf-8"))
report = json.loads(Path("examples/reports.jsonl").read_text(encoding="utf-8").splitlines()[0])
settings = resolve_model(load_config("evaluation")["llm"])
result = evaluate_rubrics_final_llm(
    query=case["query"],
    report_text=report["report"],
    rubric_objects=case["rubrics"],
    llm_settings=settings,
    output_format="score_reason_half",
)
print(json.dumps(result["rubric_results"], ensure_ascii=False, indent=2))
```

This example makes a Judge model call. The function returns parsed scores and
reasons; it does not manage CLI checkpoints or write `scores.jsonl`.

The reason template is `report_scoring_final_score_reason-half.md`.
Its `System prompt` and `User prompt template` sections supply the instructions;
reference schema/example sections are not appended to its system message.
The request consists of one system message and three user messages in the order
query → report → rubrics. Template boundaries are split before substituting user
input, so report text cannot introduce additional message boundaries.
Both System and User request reasons within 50 words. Returned reasons are whitespace-normalized,
without silent length truncation; the 50-word limit is a prompt instruction.

## External reports and input checks

Your own research system can supply reports without using our Research Framework.
See [external-report examples](../../../examples/README.md). Each JSONL row has
`case_id`, `system_id` and a nonempty `report` string; report text may include
Markdown and citations. For the benchmark, copy the exact case IDs from the
bundled `queries.jsonl`, keeping the whole generated report. JSONL escapes
newlines inside the report string. Do not substitute the query for its report.

```bash
# No credentials, Wikipedia or network required:
drbenchpro validate-reports --cases data/benchmark/cases.jsonl --reports my-reports.jsonl
# Require every system to cover all supplied cases:
drbenchpro validate-reports --cases data/benchmark/cases.jsonl --reports my-reports.jsonl --require-full
# Local setup checks; needs the configured JUDGE_* variables, no model calls:
drbenchpro preflight --stage evaluate --cases data/benchmark/cases.jsonl \
  --reports my-reports.jsonl --output outputs/my-evaluation --offline
# Optional one-request synthetic Judge test (may incur charges):
drbenchpro preflight --stage evaluate --output outputs/my-evaluation --probe-model
# Score the reports, independently of the Wikipedia service:
drbenchpro evaluate --cases data/benchmark/cases.jsonl --reports my-reports.jsonl --output outputs/my-evaluation
```

Judge probes use the installed score-only-half prompt and a trivial synthetic
satisfied rubric. They test exact rubric IDs, ternary values and the expected
answer, not real-case accuracy. They disable retries, cache and fallback, use a
30-second timeout and default 512-token output cap (`--probe-max-tokens` overrides
that diagnostic cap). Actual job settings are not changed.

Report status `truncated` is accepted and is counted by input validation;
Judge grades its visible text. It does not automatically supply zeros for a
missing or failed report. Blank reports and duplicate system/case pairs are rejected.
Reports with a different `system_id` can be evaluated together and are aggregated
separately. Comparing systems requires consistent case coverage and Judge settings.

Progress is streamed to stderr, with case/system and chunk-attempt information.
`run.json.summary` and checkpoint statistics accumulate elapsed report time,
provider token counts, API attempts/errors, chunk fallback and response truncation.
Invalid returned batches count toward usage even when their scores are rejected.
Completed-report resume does not count completed calls again. Provider usage omissions,
checkpoints without accounting and interrupted calls may make totals incomplete.
Optional paired `--input-usd-per-million` / `--output-usd-per-million` flags produce
an approximate cost; without prices `cost_estimate` is null. It is not an invoice.
The JSON summary on stdout includes both completed/failed counts and statistics.
