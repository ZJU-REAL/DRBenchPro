# DRBenchPro

DRBenchPro provides a frozen research benchmark, rubric-based report evaluation,
Breadth/Depth/Overall metrics, benchmark construction, and an optional Local
Research Framework using a fixed Wikipedia snapshot.

Research supports case selection and identity-bound resume, setup checks,
progress/accounting and CSV exports. The public evaluation CLI uses the
score-only Judge; the Python API also supports reasons.

The bundled generation framework is named **DRBenchPro Local Research
Framework**. Configurations and run metadata use the canonical protocol
identifier `local_research_v1`.

This standalone copy has not been published. The
code-license choice and data redistribution terms are still pending. See
[LICENSE](LICENSE) and [data licensing](data/README.md#licensing-and-attribution).

## Install

Use Python 3.11 or newer from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[construction,local-research,dev]'
drbenchpro --help
```

For report evaluation and metrics alone, `python -m pip install -e .` is enough.
The optional dependencies are `construction`, `local-research`, and `dev`.
The research and evaluation runners use POSIX file locks. On Windows, run them
inside WSL2 or Docker; native Windows execution of these runners is not supported.

## Offline quickstart

No model credentials or Wikipedia service are needed:

```bash
drbenchpro verify-data --registry data/registry.json
drbenchpro validate --cases data/benchmark/cases.jsonl
drbenchpro metrics   --cases examples/evaluator_case.json   --scores examples/rubric_scores.jsonl   --output outputs/example-metrics.json
```

The example returns Breadth `0.84`, Depth `0.4375`, and Overall
`0.6062177826491071`. Example reports and scores are synthetic demonstrations.
The bundled normalized benchmark contains 144 cases and 44,102 rubrics.

## Evaluate your reports

Prepare JSONL records containing `case_id`, `system_id`, and `report`, using the
case IDs in `data/benchmark/queries.jsonl`. Configure a compatible Chat Completions
endpoint and the model name valid at that endpoint:

```bash
drbenchpro init-config --output configs
cp .env.example .env
# Edit .env with your own credentials, endpoint URLs and model names.
source .env

drbenchpro evaluate   --cases examples/evaluator_case.json   --reports examples/reports.jsonl   --config configs/evaluation.yaml   --output outputs/example-evaluation

drbenchpro metrics   --cases examples/evaluator_case.json   --scores outputs/example-evaluation/scores.jsonl   --output outputs/example-evaluation/metrics.json
```

These evaluation commands make paid/external model calls when credentials are
configured. The default judge scale is `0 / 0.5 / 1`, with no report truncation.
Use your full report file and `data/benchmark/cases.jsonl` for the full benchmark.
To resume an interrupted evaluation, repeat the same command with `--resume`.
Failures are recorded explicitly; missing scores are never replaced with zeros.
See [evaluation README](src/drbenchpro/evaluation/README.md).

Validate external reports without credentials or model requests:

```bash
drbenchpro validate-reports --cases examples/evaluator_case.json --reports examples/reports.jsonl
```

Add `--require-full` for complete report coverage per system. Missing reports,
generation failures, Judge failures and actual zero rubric scores are distinct.

For the full benchmark, give each query in `data/benchmark/queries.jsonl` to your
own model or research system, then write its complete report to `my-reports.jsonl`
using the matching `case_id` and your `system_id`. Do not give the evaluator's
rubrics to the system generating the report. No Local Research Framework,
Wikipedia service, GPU or Docker installation is required for this evaluation:

```bash
drbenchpro validate-reports --cases data/benchmark/cases.jsonl --reports my-reports.jsonl --require-full
drbenchpro preflight --stage evaluate --cases data/benchmark/cases.jsonl --reports my-reports.jsonl --output outputs/my-evaluation --offline
drbenchpro evaluate --cases data/benchmark/cases.jsonl --reports my-reports.jsonl --output outputs/my-evaluation
drbenchpro metrics --cases data/benchmark/cases.jsonl --scores outputs/my-evaluation/scores.jsonl \
  --output outputs/my-evaluation/metrics.json --csv outputs/my-evaluation/systems.csv \
  --case-csv outputs/my-evaluation/cases.csv
```

Set the three `JUDGE_*` variables before preflight/evaluation. The offline checks
and metrics do not call a model; `evaluate` calls your configured Judge endpoint.
For a partial run, omit `--require-full` and explicitly add `--allow-subset` to
metrics. See the [external-report format](examples/README.md).

## Run Local Research Framework

First prepare and start the Wikipedia service as described in the
[Wikipedia README](src/drbenchpro/local_research/wikipedia/README.md).
Then configure `MODEL_API_KEY`, `MODEL_BASE_URL`, and `MODEL_NAME` in your shell:

```bash
drbenchpro research   --queries data/benchmark/queries.jsonl   --config configs/local_research.yaml   --limit 1 --system-id my-model   --output outputs/research-one

drbenchpro evaluate   --cases data/benchmark/cases.jsonl   --reports outputs/research-one/reports.jsonl   --config configs/evaluation.yaml   --output outputs/research-one-eval

drbenchpro metrics   --cases data/benchmark/cases.jsonl   --scores outputs/research-one-eval/scores.jsonl   --allow-subset --output outputs/research-one-eval/metrics.json
```

Remove `--limit 1` to generate all 144 reports. For the full run, omit
`--allow-subset` so missing cases cause an error. Selected paper model request
profiles are also copied by `init-config`; see the
[framework README](src/drbenchpro/local_research/README.md).

Select specific cases by repeating `--case-id CASE_ID`. Repeat the original
research command with `--resume` to skip exported reports and retry failed or
unattempted cases; add `--retry-failed-only` to leave unattempted cases pending.
The selected queries, model/provider configuration, system ID and implementation
must match. Resume is between cases, not within a partially executed case.

## Check setup and inspect results

```bash
# Local configuration/input/output checks only; no network calls:
drbenchpro preflight --stage research --queries data/benchmark/queries.jsonl --output outputs/research-check --offline
# Also check the fixed Wiki manifest and Search/Open; still no model calls:
drbenchpro preflight --stage research --queries data/benchmark/queries.jsonl --output outputs/research-check
# Explicit synthetic model test (may incur charges):
drbenchpro preflight --stage research --output outputs/research-check --probe-model
drbenchpro preflight --stage evaluate --cases examples/evaluator_case.json --reports examples/reports.jsonl --output outputs/judge-check --probe-model

drbenchpro metrics --cases examples/evaluator_case.json --scores examples/rubric_scores.jsonl \
  --output outputs/example-metrics.json --csv outputs/systems.csv --case-csv outputs/cases.csv
```

Preflight uses the same default configs and credentials as the corresponding job;
pass `--config` to check a custom config. It requires a fresh output path but does
not create a run. Research probes make at most three synthetic requests per route;
Judge probes make one request, without automatic retries or chunk fallback.
Probe output defaults to 512 tokens; reasoning models may need an explicitly
larger `--probe-max-tokens`. Passing a probe is a compatibility check, not proof
of performance or support for every production budget.

Research/Judge progress is flushed to stderr; final machine-readable summaries
are printed to stdout and stored in `run.json`. Research distinguishes complete,
truncated, failed and pending cases. Nonempty truncated reports remain available
for Judge. Statistics include persisted failed
attempts and retries, but interrupted calls or absent provider usage can be missing.
Reasoning tokens are included in output-token usage and must not be added again.
For an optional approximate cost, pass both `--input-usd-per-million` and
`--output-usd-per-million` to research/evaluate; prices are user-supplied and do
not alter experiment identity. Cached input uses the flat input rate.

CSV exports retain the 0–1 scale and system coverage markers; terminal metrics
show COMPLETE/PARTIAL. Full benchmark results require complete case coverage.

## Run the standalone image

The complete-image build packages the fixed Wikipedia SQLite/FTS5 database,
benchmark and application. Its default command serves Wikipedia without model
credentials. Separate commands generate reports, judge framework or external
agent reports, and calculate metrics. See the [container guide](docker/README.md)
for building, startup, model env files, preflight, case selection/resume, CSV,
full-benchmark commands and persistent output mounts.

See [hardware and resource planning](docker/README.md#hardware-and-resource-planning)
for image storage, Docker VM capacity, CPU/RAM validation status, GPU and network
requirements, and the different demands of building, verification and runtime.

The complete image targets linux/amd64 and includes a 160.2 GiB database; a local
tag requires a build or image transfer before use. Do not mount over its built-in
Wikipedia directory. Research requires tool-capable OpenAI-compatible Chat
Completions; Judge uses independent credentials. Model revisions and sampling
can affect generated reports and scores.

## Run the application-only serving image

The repository includes an application image for the Research API and the local
Wikipedia service. The image contains code and dependencies; the fixed SQLite
index remains an external read-only mount.

```bash
cp .env.example .env
# Set DRBP_SERVICE_API_KEY and check DRBP_WIKI_DATABASE in .env.
docker compose build
docker compose up -d
curl http://127.0.0.1:8090/ready
```

The Research API is available at `http://127.0.0.1:8090/v1/research`. The model
request must use a server-approved route when strict policy is enabled. Model API
credentials are supplied at runtime and are never copied into the image.

## Construct new cases

```bash
drbenchpro construct --seed 'Ada Lovelace'   --config configs/construction.yaml --output outputs/new-cases
```

This performs online Wikipedia fetches and model calls. It creates a **new**
case dataset; regenerating the bundled frozen benchmark from live Wikipedia
is not guaranteed. See [construction README](src/drbenchpro/construction/README.md).

## Code and documentation

| Directory | Implementation and usage |
| --- | --- |
| [data](data/README.md) | Bundled dataset, schemas, IDs, integrity and licensing |
| [construction](src/drbenchpro/construction/README.md) | Graph → sampled tree → query/rubrics |
| [evaluation](src/drbenchpro/evaluation/README.md) | Report interface, judge and checkpoint runner |
| [metrics](src/drbenchpro/metrics/README.md) | Canonical case and system metrics |
| [local_research](src/drbenchpro/local_research/README.md) | Planner, research units, gap analysis and synthesis |
| [wikipedia](src/drbenchpro/local_research/wikipedia/README.md) | Snapshot download, indexing and Search/Open |

There is no separate `docs/` tree. Prompts/configurations that must survive
installation live under `src/drbenchpro/resources/`. Runtime paths are supplied
through the CLI/config, and imports use the installed `drbenchpro` package.
No `PYTHONPATH` changes or access to the original development repository are needed.

## Reproducibility and scope

The package includes deterministic metrics, the normalized frozen benchmark,
Judge prompts, selected research-model request settings and the Local Research
Framework. The application-only image uses an external read-only index mount;
the standalone build embeds the finalized database and manifests. Original
Wikipedia dump files are not needed at runtime.

Research requires the fixed snapshot/index and a compatible model endpoint.
Evaluation requires a compatible Judge endpoint; metrics require no model access.
Keep the data identity, model/provider settings, prompts, budgets and metric
parameters consistent when comparing runs. Model sampling and provider revisions
can change reports and scores; identical outputs are not guaranteed.
Record the Python and dependency versions with your results. Across Python
versions, metric JSON can differ in the last floating-point digits; compare
cross-environment results with a numerical tolerance. Python 3.12 changed
[floating-point summation](https://docs.python.org/3/whatsnew/3.12.html#other-language-changes).

## Packaging

```bash
python -m build
```

The wheel contains code and prompt/config resources. The source distribution
also contains the benchmark and user examples. Use the source distribution
for the complete standalone release project.

## Version and citation

Code: `0.3.0`; data: `drbenchpro-144-v1-release-schema-v1`; metric:
`drbenchpro-coverage-v1` with `beta=0.60, alpha=0.25, eta=0.75, rho=0.50`.
The Judge uses ternary scores (`1`, `0.5`, `0`), while metric computation maps
only `1` to covered and maps `0.5` and `0` to uncovered. Breadth and Depth are
evaluated bottom-up over the actual rubric tree; shallower leaves use their direct
binary score without applying a non-leaf update.
The authoritative paper citation/URL is not yet finalized in this release;
no fabricated BibTeX or publication identifier is supplied.

### Current research budgets

The release uses 8,192 output tokens for research-unit rounds and evidence summaries,
48,000 characters for Gap evidence, and 80,000 characters for synthesis evidence.
Planner/Gap output is limited to 4,096 tokens; final report output to 32,768.
These settings define the current execution conditions. See the
[framework README](src/drbenchpro/local_research/README.md) for configuration fields.
