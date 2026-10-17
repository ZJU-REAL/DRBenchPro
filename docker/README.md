# DRBenchPro Local Research Framework — Docker Image

`Dockerfile.bundled` packages the application, frozen benchmark, fixed Wikipedia
SQLite/FTS5 database, manifests and Wikipedia license notices. The original XML
and SQL dump files are not needed at runtime. The prebuilt image is available on
[Hugging Face](https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro-runtime).
It includes all application dependencies and data; users do not rebuild the
index, install DRBenchPro on the host, or clone the development repository.

The repository address retains `DRBenchPro-runtime`; the user-facing resource
is **Framework Image**. It is a downloadable Docker archive, not an HF-hosted
online service or a registry image that can be pulled directly with `docker pull`.
It does not contain model weights or API credentials. For evaluating your own
reports without the framework, use the Python evaluator and the separate
[benchmark dataset](https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro).

## Download the Framework Image

Use Docker, Bash, and Python 3.10+ on the host for the download/import helpers.
Install the HF CLI into your own environment if needed, then download the fixed
release and its helpers:

```bash
python -m pip install 'huggingface_hub>=2.2,<3'
hf download zhuiluozhiji/DRBenchPro-runtime \
  --repo-type dataset --revision ec0905aa0d18a4882bb92a59d445ab1ca4cdddfb \
  --include 'images/0.3.0-enwiki-20260701-v1-8733db7-linux-amd64/*' \
  --include 'scripts/*' --local-dir DRBenchPro-runtime
hf download zhuiluozhiji/DRBenchPro-runtime \
  LICENSE CODE_LICENSE DATA_LICENSE NOTICE.md \
  licenses/CC-BY-SA-4.0.txt data/WIKIPEDIA_SOURCES.txt \
  --repo-type dataset --local-dir DRBenchPro-runtime
bash DRBenchPro-runtime/scripts/verify.sh
bash DRBenchPro-runtime/scripts/load.sh
export DRBP_IMAGE=drbenchpro/standalone:0.3.0-enwiki-20260701-v1-8733db7
docker image inspect "$DRBP_IMAGE" --format '{{.Id}} {{.Os}}/{{.Architecture}}'
```

Expected identity: `sha256:a5b1f5a2608cf8258a8ebc796f745d775c772d4bccc9faf0bf88410d95f20e37`
and `linux/amd64`. The embedded source revision is
`8733db79a6e51b4716457daa9df4567b115932fe`; the fixed Wikipedia snapshot is
`enwiki-20260701-v1`.

Keep the accompanying license files with the archive. `CODE_LICENSE` grants MIT
for the original DRBenchPro software in this identified image, copyright 2026
REAL Lab; it also supplies the applicable code grant when `/app/LICENSE` contains
a release-status notice. `DATA_LICENSE` supplies CC BY-SA 4.0 for project-owned
contributions to the identical bundled benchmark; the accompanying grants
supersede pending-license statements for those components in the frozen image
or release notices. Wikipedia and third-party components retain their terms.
The payload and executable helpers use the fixed revision
above; the second command retrieves the accompanying licensing documents.

The download is **12 parts, 63,017,845,747 bytes (58.69 GiB)**, forming one
gzip-compressed Docker archive. All parts are required. The helpers verify each
part and the complete archive before streaming it into Docker, then verify the
loaded image identity and restore its tag. An extra combined archive is not
created. Rerun the same download command after interruption, preserving the
same revision, directory and download metadata. Public downloads require no HF token.

Read the resource planning below before import. After import, continue with
[Start Wikipedia](#start-wikipedia), then the Research, evaluation and metrics
commands in this guide. Users of the prebuilt image can skip
[Build](#build). The HF [Framework Image card](https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro-runtime)
and its matching [container guide](https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro-runtime/blob/main/container-guide.md)
also describe this release.

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
| Image storage | The published image has a reported size of 172,187,242,727 bytes (160.36 GiB), including a 160.2 GiB database. Allow more space for the image store, extraction, temporary files and user outputs. Actual disk use depends on the storage backend and layer sharing. |
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

## Start Wikipedia

On a host with the imported Framework Image (or set `DRBP_IMAGE` to your own
verified local build):

```bash
export DRBP_IMAGE=drbenchpro/standalone:0.3.0-enwiki-20260701-v1-8733db7
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
Research uses three base logical calls per configured route, with at most one
correction per Search/Open step and one explicit thinking/tool-choice parameter
repair per logical call (at most nine HTTP attempts per route). Judge uses one.
Probes use 30-second timeouts without network retries or route fallback; thinking
settings are preserved and compatibility/correction counts are reported. Default output is
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

## Build

This advanced path is for building a new image from source. To use the published
Framework Image, follow [download and import](#download-the-framework-image).

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

Original DRBenchPro software is MIT-licensed, copyright 2026 REAL Lab. New builds
include `LICENSE`, `DATA_LICENSE` and
`licenses/CC-BY-SA-4.0.txt` under `/app/`, and the installed package includes
these notices in its distribution metadata. The bundled benchmark's source
list is `/opt/drbenchpro/data/WIKIPEDIA_SOURCES.txt`. Benchmark contributions
use CC BY-SA 4.0 within the stated scope; Wikipedia notices and third-party licenses
apply to their own components. The HF release supplies an accompanying
`CODE_LICENSE` for the software in its identified image. A local build does not
publish the image to a registry.

The index uses the English Wikipedia dump dated 2026-07-01
(https://dumps.wikimedia.org/enwiki/20260701/). Source text is credited to
Wikipedia contributors. Retain `/opt/drbenchpro/wiki/WIKIPEDIA_CONTENT_NOTICE.md`
and the upstream license texts under `/opt/drbenchpro/licenses/` when
redistributing the corresponding content. Python, operating-system packages
and other dependencies retain their respective upstream licenses.

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
