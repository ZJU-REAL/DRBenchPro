# Standalone image

`Dockerfile.bundled` packages the application, frozen benchmark, fixed Wikipedia
SQLite/FTS5 database, manifests and Wikipedia license notices. The original XML
and SQL dump files are not needed at runtime. Users of a built image do not
download data, build an index, install Python or clone the development repository.

The target platform is **linux/amd64**. The database alone is 172,016,893,952 bytes
(160.2 GiB); allow additional disk space for image storage and download/extraction.
Building from a data stage can create a second large layer and temporary copies;
reserve at least 500 GiB of additional space before a full build and recheck the
actual Docker filesystem. Apple Silicon requires amd64 emulation. Native ARM
support is not established.

The build requires BuildKit support for `COPY --link` and `COPY --chmod`. The
database uses an independent layer that the builder can reuse across application
updates. Dependency installation disables bytecode compilation; project bytecode
caches are not included in the image.

Code and benchmark license decisions are pending. Included Wikipedia notices
and license texts apply to Wikipedia content, not automatically to the project
code or generated benchmark. A local build does not publish the image to a registry.

## Hardware and resource planning

The supported image platform is **linux/amd64**. A compatible container runtime
and sufficient space in its image filesystem are required. Apple Silicon uses
amd64 emulation; miniature tests do not establish full-database performance on
ARM hosts. On Docker Desktop, check both physical host free space and the Linux
VM's disk capacity, CPU and memory allocation. A large virtual disk can report
more free space than the physical host can actually provide. See the official
[Docker Desktop resource settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/).

| Resource | Requirement or planning guidance |
| --- | --- |
| GPU | Not required when Research and Judge call external model APIs. The image does not host model weights; requirements for a separately hosted model are additional. |
| Image storage | The built image has a reported size of 172,173,253,119 bytes (160.35 GiB), including a 160.2 GiB database. Allow more space for the image store, extraction, temporary files and user outputs. Actual disk use depends on the storage backend and layer sharing. |
| Initial pull/load | About 350 GiB of free space is a planning allowance for roughly two image-sized copies plus headroom, not a measured peak or certified minimum. Recheck the actual backend; a retained archive consumes additional host space according to its actual size. |
| Building from source | Reserve at least 500 GiB of additional space for the large data layer, build cache and temporary copies. This is a planning allowance, not a measured build peak. |
| CPU and RAM | A validated minimum and recommended configuration have not yet been established. Successful functional tests on a large host do not establish requirements for a smaller machine. Start with one case and observe resource use before increasing concurrent jobs. |
| Storage performance | SSD storage is preferable for SQLite/FTS access. Database copying, image extraction and complete verification perform substantial disk I/O. |
| Network | Local Wiki retrieval needs no Internet connection. Research/Judge require access to the configured external model endpoints; offline metrics do not. |

The database is read through SQLite connections; its file size is not a RAM
requirement. Filesystem caching and SQL temporary results still use memory, and
concurrent retrieval requests or multiple jobs can increase demand. Research
uses three concurrent units per case by default; the CLI processes selected
cases in sequence. Separate jobs add their own resource and API demand.

| Stage | Main resource demand |
| --- | --- |
| Build, pull or load the bundled image | Large disk allocation, copying/extraction I/O and transfer time; compression/extraction can also use CPU. |
| Complete file/SQLite/FTS verification | Long database scans, disk I/O and CPU. This maintenance operation is separate from normal startup. |
| Wiki serving during Research | Search CPU, SQLite disk access and memory/cache; concurrent retrieval increases demand. Startup and health checks do not scan the whole database. |
| API-backed Research and Judge | Provider latency, rate limits and token usage, plus local orchestration and output files. No local GPU inference is performed. |
| Metrics and CSV | Local CPU/RAM for benchmark and score aggregation; no Wiki service or model API is required. |

Observe running containers with `docker stats`, but do not treat one sample as a
peak or minimum. The Linux Docker CLI subtracts cache from its memory display;
account for filesystem cache and other containers when assessing host capacity.
See [Docker statistics](https://docs.docker.com/reference/cli/docker/container/stats/).
Minimum CPU/RAM guidance requires resource-limited tests with representative
queries and concurrency, including cold-cache behavior. Real-provider throughput
and separately hosted model requirements need their own measurements.

## Build

Use a Docker daemon that already holds the verified, complete data image. Inspect
the data image, embedded manifests and database integrity before selecting it.
Never use a partial index as a full snapshot. Build with committed source:

```bash
export WIKIPEDIA_DATA_IMAGE=drbenchpro/wiki-search-enwiki:20260701-v1-bundled
# Set this to the full sha256 image ID you verified, not a guessed value:
export WIKIPEDIA_DATA_IMAGE_ID=sha256:YOUR_VERIFIED_64_HEX_IMAGE_ID
bash docker/build-bundled.sh
```

The helper checks the exact local data-image ID and amd64 platform, uses a
Docker-driver builder with access to it, pins the available Python base repository
digest, and prints build identities. It never pulls an unknown data image. Set
`DRBP_BUILDER` to another Docker-driver builder if necessary. `PYTHON_IMAGE` may
select an approved Python 3.12 slim base; it must resolve to linux/amd64 and have a
repository digest. Actual Python and dependency versions must be recorded for a
build: pyproject dependency ranges alone do not guarantee identical environments.

Git archives/source distributions lack Git metadata: set `DRBP_SOURCE_REVISION`
to the full commit from which the source was exported. In a checkout, omit this
variable to require clean committed public source. The application-only
`Dockerfile` remains available for deployments mounting an external index.

The default output tag is `drbenchpro/standalone:0.3.0-enwiki-20260701-v1`.
`DRBP_IMAGE` can choose a different local output tag. Registry publishing or
archive export is separate from building; the tag is usable on a host only after
the image has been built, loaded or pulled there.

## Start Wikipedia

On a host with the built image:

```bash
export DRBP_IMAGE=drbenchpro/standalone:0.3.0-enwiki-20260701-v1
mkdir -p outputs config
docker run -d --name drbp-standalone \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  "$DRBP_IMAGE"
docker inspect --format '{{.State.Health.Status}}' drbp-standalone
docker logs drbp-standalone
```

Wait for `healthy`. Default startup only serves Wikipedia on port 8091: no model
credentials, Research or Judge calls. Startup and health checks require the
complete canonical 71-shard index and matching embedded/sidecar/service manifests;
health checks do not repeatedly hash or scan the 172 GB database.
Do not mount a volume over `/opt/drbenchpro/wiki` or copy the database to outputs.
Containers using the same image share its read-only data layers.

The guide and blank Docker env examples are also available inside the image at
`/app/docker/`, for example `docker run --rm --network none --entrypoint cat
"$DRBP_IMAGE" /app/docker/README.md`.

The following optional maintenance command checks SQLite, FTS counts and tools;
on a full index it can take considerable time. It does not call a model or
recompute the file's SHA-256:

```bash
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m "$DRBP_IMAGE" verify-wiki
docker run --rm --network none "$DRBP_IMAGE" verify-data
docker run --rm --network none "$DRBP_IMAGE" validate
```

## Configure models

Create separate Docker env files using the provided examples:

```bash
cp docker/research.env.example research.env
cp docker/judge.env.example judge.env
```

Use plain `KEY=value` lines, without `export`. Research requires `MODEL_API_KEY`,
`MODEL_BASE_URL` and `MODEL_NAME`; Judge requires `JUDGE_API_KEY`, `JUDGE_BASE_URL`
and `JUDGE_MODEL`. Both use OpenAI-compatible Chat Completions API base URLs,
without the `/chat/completions` suffix. Research also requires compatible
function/tool calling. Other native API protocols require their own adapters.
Do not commit the populated files or put them in a build context.

Optional YAML configuration:

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/config,dst=/config" \
  "$DRBP_IMAGE" init-config --output /config
```

Existing config files are not overwritten. Advanced provider/reasoning settings
remain explicit YAML fields. Mount config read-only and add
`--config /config/local_research.yaml` or `--config /config/evaluation.yaml` to the
corresponding job; generic runs need no exported YAML.

## Preflight

```bash
# Local checks only, zero network/model requests:
docker run --rm --network none --user "$(id -u):$(id -g)" --env-file research.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" preflight --stage research \
  --queries /opt/drbenchpro/data/benchmark/queries.jsonl \
  --output /outputs/research-preflight --offline

# Also verify manifest, Search and Open; still zero model calls:
docker run --rm --network container:drbp-standalone \
  --user "$(id -u):$(id -g)" --env-file research.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" preflight --stage research \
  --queries /opt/drbenchpro/data/benchmark/queries.jsonl \
  --output /outputs/research-preflight
```

Only explicit `--probe-model` sends synthetic model requests and can incur charges.
Research uses at most three requests per configured route; Judge uses one.
Probes use 30-second timeouts without automatic retry/fallback. Default output is
512 tokens; `--probe-max-tokens 8192` changes only the probe limit. Probes never
use benchmark rubrics as input to a research model or change formal budgets.

## Generate reports

```bash
docker run --rm --network container:drbp-standalone \
  --user "$(id -u):$(id -g)" --env-file research.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" research \
  --queries /opt/drbenchpro/data/benchmark/queries.jsonl \
  --system-id my-model --limit 1 --output /outputs/research-one
```

The shared network lets the existing generic YAML reach Wikipedia at
`http://127.0.0.1:8091`. Research uses query-only input and exposes only Search/Open
to the model; evaluator cases/rubrics are not model input. Local Wiki traffic
bypasses public proxies. Changing container topology requires an explicit Wiki
URL in YAML; `DRBP_WIKI_BASE_URL` configures the optional API launcher, not the
research CLI. Model proxies require explicit per-job proxy variables and the
client's `disable_env_proxy` setting.

Repeat the identical command with `--resume` to skip accepted reports and retry
failed/pending cases. Add `--retry-failed-only` to retry failures while leaving
unattempted cases pending. Replace `--limit 1` with repeated `--case-id CASE_ID`
to select specific cases and use a new output directory. Selection, system,
model/provider settings, budgets and implementation must match on resume.
Credential rotation does not change identity. Recovery is between cases; an
interrupted case is run again from its beginning. Preserve run/checkpoint,
attempts and runs files. Missing/modified attempts and concurrent writers fail.

Reports, failures, status and statistics persist in host `outputs/research-one`.
Nonempty truncated reports are exported with their status; other failures remain
separate and cause a nonzero exit code. Stage progress goes to stderr, final JSON
to stdout and run.json. Optional paired `--input-usd-per-million` and
`--output-usd-per-million` estimate cost from provider-reported tokens; reasoning
is included in output and is not added twice. Estimates are not bills, and
unreported/interrupted usage can be missing.

## Validate external reports and run Judge

Other models/agents may provide their own JSONL reports. Each record contains
`case_id`, `system_id`, and nonempty `report`; case IDs come from the built-in
queries. Put reports in host `outputs/external/reports.jsonl`, then validate:

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" validate-reports \
  --cases /opt/drbenchpro/data/benchmark/cases.jsonl \
  --reports /outputs/external/reports.jsonl
```

Add `--require-full` to require every case for each supplied system.
Unknown IDs, duplicate system/case pairs and blank reports fail. Generation
failures, missing reports, Judge failures and real zero scores remain distinct.

Judge preflight, without Wiki or model requests:

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" --env-file judge.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" preflight --stage evaluate \
  --cases /opt/drbenchpro/data/benchmark/cases.jsonl \
  --reports /outputs/research-one/reports.jsonl \
  --output /outputs/judge-preflight --offline
```

To explicitly probe Judge, remove `--network none`/`--offline`, add `--probe-model`.
To score, run:

```bash
docker run --rm --user "$(id -u):$(id -g)" --env-file judge.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" evaluate --cases /opt/drbenchpro/data/benchmark/cases.jsonl \
  --reports /outputs/research-one/reports.jsonl --output /outputs/evaluation-one
```

For external systems substitute `/outputs/external/reports.jsonl`. Judge needs
only Judge credentials and no Wikipedia/Research container. Repeat the identical
command with `--resume` to skip completed reports. Invalid/missing rubric scores
fail without replacing them with zeros. Scores/checkpoint/run persist on the host.

## Metrics and full benchmark

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" metrics --cases /opt/drbenchpro/data/benchmark/cases.jsonl \
  --scores /outputs/evaluation-one/scores.jsonl --allow-subset \
  --output /outputs/evaluation-one/metrics.json \
  --csv /outputs/evaluation-one/systems.csv --case-csv /outputs/evaluation-one/cases.csv
```

Metrics need no credentials/network. JSON and CSV retain 0–1 values; terminal
summaries identify COMPLETE/PARTIAL coverage. Output paths cannot overwrite inputs
or one another. A single case is a partial result, not a full benchmark score.

Full benchmark workflow (model calls can incur charges):

```bash
docker run --rm --network container:drbp-standalone \
  --user "$(id -u):$(id -g)" --env-file research.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" research --queries /opt/drbenchpro/data/benchmark/queries.jsonl \
  --system-id my-model --output /outputs/research-full
docker run --rm --user "$(id -u):$(id -g)" --env-file judge.env \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" evaluate --cases /opt/drbenchpro/data/benchmark/cases.jsonl \
  --reports /outputs/research-full/reports.jsonl --output /outputs/evaluation-full
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/outputs,dst=/outputs" \
  "$DRBP_IMAGE" metrics --cases /opt/drbenchpro/data/benchmark/cases.jsonl \
  --scores /outputs/evaluation-full/scores.jsonl \
  --output /outputs/evaluation-full/metrics.json \
  --csv /outputs/evaluation-full/systems.csv --case-csv /outputs/evaluation-full/cases.csv
```

Omitting `--allow-subset` requires all 144 cases. Use the same case coverage and
Judge configuration when comparing systems. Host `--user UID:GID` lets a Linux
user write their own bind mount; the default image UID is 10001. Keep config
mounts read-only and outputs writable. No permission changes to data or historical
output directories are needed.

## Stop and restart

```bash
docker stop drbp-standalone
docker start drbp-standalone
```

The entrypoint execs one process, so SIGTERM reaches the service. Job containers
can be removed without deleting host outputs. The optional `research-api` command
starts the separate Research HTTP API using `/outputs/api` and localhost Wiki;
share the Wiki container network and supply the required service/model policy.
The CLI workflow does not require that API or promise a strict route policy.
