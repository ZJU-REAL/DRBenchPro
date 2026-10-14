# Local Research Framework

Install `.[local-research]`. This package implements the DRBenchPro Local Research
Framework: Planner → concurrent Units → optional Gap → Synthesis. Wikipedia
retrieval is nested here because it is this framework's tool environment. Scoring
and standalone metric computation do not require a Wikipedia service.

```bash
# After setting up the Wikipedia service and exporting MODEL_*:
drbenchpro research --queries data/benchmark/queries.jsonl   --config configs/local_research.yaml --limit 1 --system-id my-model   --output outputs/research-one
```

`configs/local_research.yaml` is a generic compatible-endpoint preset. Set
`MODEL_API_KEY`, `MODEL_BASE_URL`, `MODEL_NAME`; edit `wiki.base_url` for your local
service. `report.system_prompt` configures final-report instructions;
reports must contain at least 300 words. Generic research temperature is 0.2.

## Standalone Docker workflow

The [container guide](../../../docker/README.md) describes the image with a
built-in fixed Wikipedia index and benchmark. Its default command serves Wiki;
separate `research`, `evaluate` and `metrics` jobs persist host outputs. Research
jobs share the Wiki container's network and use localhost in the default YAML.
Judge and metrics do not require Wiki. Docker env examples use plain `KEY=value`
lines; the shell `.env.example` uses `export` and is a separate format.

## Application-only Docker serving

The repository-level `Dockerfile` builds an application image containing the
Research API and the Wikipedia service command. The `compose.yaml` starts both
services from that image. Mount a compatible `enwiki-20260701-v1` SQLite index at
`DRBP_WIKI_DATABASE` and persist service runs through `DRBP_RUNS_DIR`:

```bash
cp .env.example .env
docker compose build
docker compose up -d
curl http://127.0.0.1:8090/ready
```

The Research API launcher is `drbp-research-serve`. It reads `DRBP_RUN_DIR`,
`DRBP_WIKI_BASE_URL`, `DRBP_SERVICE_API_KEY`, and the capacity settings from the
environment. The image does not contain credentials, a Wikipedia dump, or a
prebuilt index.

`init-config` also exports `local_research_<model>.yaml` for seven selected paper
models: DeepSeek V4 Pro/Flash, Qwen3.7 Plus, Qwen3.8 Max, MiniMax M3, Gemma4 31B and
GLM5.2. These pin model IDs and set provider routing constraints and reasoning
settings in `request_overrides`; they expect a compatible OpenRouter endpoint.
Credentials and endpoint URLs come from environment variables. Check endpoint
compatibility with `preflight` before starting a benchmark run.
Changing a pinned model or routing constraint is a different experiment.

## Default budgets

| Setting | Value |
| --- | --- |
| Initial / maximum research units | 3 / 4 |
| Unit concurrency / maximum rounds | 3 / 7 |
| Gap checks | at most 1 |
| Searches / opens / tools per unit | 3 / 12 / 15 |
| Opens per turn | 4 |
| Characters per open / per unit | 6,000 / 72,000 |
| Shared initial / gap / total tool budget | 60 / 12 / 72 |
| Planner / Gap output-token limit | 4,096 |
| Research-unit / evidence-summary output-token limit | 8,192 / 8,192 |
| Final report output-token limit | 32,768 |
| Gap / synthesis evidence-character limit | 48,000 / 80,000 |
| Final report attempts / unit retries | 3 / 1 |

At least one unit must be grounded; all units need not succeed. Grounding,
citation validation, failed-unit markers and shared-budget accounting apply to
each run. Provider/native web search is forbidden; only the
local Search/Open contract is allowed. The research model client's `max_retries`
means retries **after** the first attempt (unlike the judge client's total-attempt
setting). The selected profiles specify retry backoff and reasoning/provider
settings. Model output limits do not guarantee that a provider accepts those limits.

## Results

Each run writes `reports.jsonl`, `failures.json`, `run.json` and per-case artifacts
under `runs/`. Successful reports and nonempty `truncated` reports are exported,
with their status retained. Other failures are kept out of the scoring input and result in a
nonzero exit code. New runs require an empty output directory; existing runs use
the identity-bound `--resume` described below. The service/persistence
API is available for applications needing it.

Research prompts are defined in `agents.py` and `report.system_prompt`.
The package `service.py` exposes `create_app(...)`; the CLI calls the engine
directly and requires no internal gateway or account scheduler.

See [Wikipedia setup](wikipedia/README.md) for the required snapshot/index.
Research also requires compatible model access.

## Budget configuration

Under `limits`, `research_output_tokens` and `summary_output_tokens` control
unit-loop calls and the explicit final summary round independently. An early
summary produced during a research round uses the research-round budget.
`max_model_output_tokens_per_call` controls Planner and Gap output.
`gap_evidence_chars` and `synthesis_evidence_chars` bound the assembled evidence.

Input evidence is limited by character slicing. Model output limits include
reasoning tokens when the provider counts them toward its completion budget;
they do not guarantee an equally long visible summary. Changing budgets changes
the execution conditions and requires a new output directory.

## Protocol naming

The only accepted protocol identifier is `local_research_v1`; configurations and
run metadata use that identifier. Other identifiers are rejected
instead of being silently normalized.

The framework API version `1.0.0` identifies the serving interface;
`local_research_v1` identifies the experiment protocol. The installed package
version is available from `drbenchpro.__version__`.

## Case selection and safe resume

```bash
drbenchpro research --queries data/benchmark/queries.jsonl --system-id my-model \
  --case-id CASE_ID_1 --case-id CASE_ID_2 --output outputs/selected
# Repeat the SAME query selection, system and config:
drbenchpro research --queries data/benchmark/queries.jsonl --system-id my-model \
  --case-id CASE_ID_1 --case-id CASE_ID_2 --output outputs/selected --resume
# Retry recorded failures only, leaving unattempted cases pending:
drbenchpro research --queries data/benchmark/queries.jsonl --system-id my-model \
  --case-id CASE_ID_1 --case-id CASE_ID_2 --output outputs/selected --resume --retry-failed-only
```

Replace IDs with values in `queries.jsonl`. Selection preserves dataset order;
`--limit` applies after selection. Unknown/duplicate IDs fail before model calls.
Successful and nonempty truncated reports are skipped on resume. Failed or
unattempted cases are rerun from their first phase with the same per-case budgets.
Resumption never joins new research evidence to a partially executed case.
Explicit case selection with a new output directory can rerun any chosen case.
Changing the selection on an existing run requires a new directory.

The identity includes selected input, system ID, resolved non-secret model and
provider configuration, protocol/budgets, and research implementation/prompt
source hashes. API-key rotation does not invalidate it. Changed input/config/code
is rejected before calls. Resume requires a valid matching checkpoint identity;
directories without it require a new output directory.

Full immutable attempt artifacts live in `attempts/<case-position>/<attempt>.json`;
`runs/` contains the latest full result per case. Checkpoint/export files are
written atomically and can be regenerated from attempts after interruption.
Missing/modified checkpoint-referenced attempts are rejected. A process lock
rejects concurrent writers to one output directory (Linux/macOS local filesystems;
use separate output directories for independent jobs).

Case-level recovery cannot recover a model call that completed upstream but was
interrupted before its case artifact was saved. Repeating that case can incur
additional charges. Attempt history retains both failed and replacement results;
`reports.jsonl` contains only the latest accepted report per case, without duplicates.
Exit status is nonzero while failures or unattempted cases remain. A truncated
export is accepted and is counted separately.

## Preflight and statistics

```bash
drbenchpro preflight --stage research --config configs/local_research.yaml \
  --queries data/benchmark/queries.jsonl --output outputs/next-run --offline
drbenchpro preflight --stage research --config configs/local_research.yaml \
  --queries data/benchmark/queries.jsonl --output outputs/next-run
drbenchpro preflight --stage research --config configs/local_research.yaml \
  --output outputs/next-run --probe-model --probe-max-tokens 8192
```

All checks require runtime model configuration/credentials. `--offline` performs
no network calls. Normal preflight additionally validates the fixed Wiki manifest,
searches for `research` and opens one short result; these diagnostic results are
never added to a benchmark run or its tool budget. Only `--probe-model` sends
synthetic evidence to a model. It checks Search/Open tool calls, preservation of
source IDs and visible output, with up to three requests per configured route,
30-second request timeouts and no automatic retries/fallback. Tests of forced
specific tool choice are deliberately strict: a failed probe is diagnostic, not
proof the model cannot use the framework's normal auto tool selection.
A 512-token probe can exhaust a reasoning model's output budget; increase
`--probe-max-tokens` explicitly when investigating that failure. Never silently
change the production reasoning settings to make a probe pass.

Progress JSON lines go to stderr, including current case, phase, completion
status and time. The final stdout/run.json summary separates complete, truncated,
failed and pending cases and counts persisted attempts, retries, recovery events,
truncated model calls, Search/Open and provider-reported usage. Interrupted work
can be absent from the totals; provider omissions are not guaranteed zero cost.
For optional cost estimation supply both `--input-usd-per-million` and
`--output-usd-per-million`; reasoning is not billed a second time in the estimate,
all cached input uses the input rate, and different route tariffs are not inferred.
The price estimate is separate from the experiment identity and actual invoice.
