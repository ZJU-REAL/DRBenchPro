# Benchmark construction

Install `.[construction]`, export `MODEL_API_KEY`, `MODEL_BASE_URL`, `MODEL_NAME`,
and copy package configurations using `drbenchpro init-config --output configs`.

```bash
drbenchpro construct --seed 'Ada Lovelace'   --config configs/construction.yaml --output outputs/ada
```

This command accesses live Wikipedia and invokes the configured model. Outputs
include `graph.json`, selected subgraphs, query inputs, queries, edge evidence,
rubrics, normalized `cases.jsonl`, query-only `queries.jsonl`, and `run.json`.
Only complete, validated cases are exported; failed/fallback rubrics block export.
Exported rubrics use `parent_rubric_id` (null at depth 1) to express their tree.
Graph edge IDs are mapped to rubric IDs during export. Edge IDs and relation
descriptions remain in construction intermediates, outside the formal cases.
Intermediate artifacts are available for diagnosis. The public command requires
a new empty output directory and does not support resume.

The default structural profile uses depth 3, root target 50 neighbors, up to 5
neighbors per non-root, and 3 selected subgraphs with 10 first-hop branches each.
Graph neighbors and subgraphs are selected by LLM, queries use the anti-leakage
`less` prompt, and rubric batches request 32 edges. Root/non-root candidate pools
are 512/128. Parameters come from the current construction profile, with modest
local fetch concurrency/retry defaults. Page availability depends on live Wikipedia.

The implementation is organized into these subpackages:

| Path | Role |
| --- | --- |
| `graph/` | Wikipedia client/parser, graph builder, neighbor/relation selection |
| `subgraphs/` | Depth-1 branch-tree sampling, topology and evidence materialization |
| `generation/` | Query and edge-grounded rubric generation |
| `stages/` | Graph/sampling/query/rubric stage entry points |
| `pipeline.py` | Public orchestration and validated case export |

`stages/build_sample.py` provides the query stage and its dependencies. Internal
rule helpers are distinct from the public LLM construction preset.

Templates live in package `resources/prompts/construction/`. Root selection,
neighbor selection, depth-1 branch selection, anti-leakage query generation and
batch rubric generation send the `System prompt` and `User prompt template`
sections. Their reference schemas and examples are documentation and are not
appended to model messages. Other templates can include output schemas,
requirements and examples when composing their system messages.
Model inputs for query generation are separate from the evaluator rubric set.

Constructed datasets have their own identity. Use the bundled frozen benchmark
when comparing model scores on the released benchmark. Live Wikipedia edits and
LLM sampling mean new construction is not byte-identical regeneration of v1.
