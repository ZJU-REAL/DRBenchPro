# Data, interfaces and integrity

The local release contains `benchmark/cases.jsonl` and the strictly query-only
`benchmark/queries.jsonl`. Run `drbenchpro verify-data --registry data/registry.json`
and `drbenchpro validate --cases data/benchmark/cases.jsonl` from the repository
root. There is no public download URL yet; the registry explicitly records null.
The bundled cases are uncompressed UTF-8 JSONL, with one complete case per line,
so users can read and inspect them without a decompression step.

## Dataset identity

This is the normalized `drbenchpro-144-v1-release-schema-v1` distribution:
144 cases and 44,102 rubrics, with query/rubric text and linked tree topology.
`registry.json` identifies the distribution and supplies SHA-256 hashes for its
bundled files. Use `verify-data` to check them before evaluation.
The code package wheel does not embed this dataset; the full source archive does.

## Records

Agent input is JSONL with `case_id` and `query`. Evaluator input is one JSON case
or JSONL/gzipped JSONL cases with this schema:

```json
{
  "case_id": "example-001",
  "query": "A research question",
  "rubrics": [
    {"rubric_id": "r1", "rubric_text": "A required fact",
     "rubric_depth": 1, "parent_rubric_id": null},
    {"rubric_id": "r2", "rubric_text": "A dependent fact",
     "rubric_depth": 2, "parent_rubric_id": "r1"}
  ]
}
```

Rubric IDs must be unique within a case. Depth is 1–3; each depth-1 rubric has
an explicit null `parent_rubric_id`, and each deeper rubric references exactly one
parent rubric at the previous depth. Missing parents, duplicates, and incomplete
score coverage are errors. IDs need not be globally unique:
join scores on `(system_id, case_id, rubric_id)`.

Optional `domain`, `source_title` and `target_title` fields describe the case and
its article relationships. Formal cases use rubric IDs directly for topology;
they do not contain `edge_id`, `parent_edge_ids`, `relation_label` or
`relation_explanation`. The Judge receives only each rubric's ID and text.
Cases are validated directly by their required fields and tree structure;
neither individual cases nor the data registry require a format-version field.

A report row is `{"case_id":"example-001","system_id":"my-model","report":"..."}`.
A score row is `{"case_id":"example-001","system_id":"my-model","rubric_id":"r1","score":0.5}`.
The score scale is exactly 0, 0.5, or 1. Extra descriptive fields are ignored by the
metric implementation. `system_id` defaults to `default` when absent.
The released coverage metric preserves these ternary Judge rows as inputs but
maps only `1` to covered; both `0.5` and `0` are mapped to uncovered before tree
aggregation, following the paper's binary coverage definition.

Never pass evaluator rubrics/trees to the research model. The research CLI
explicitly rejects evaluator fields and uses only case ID and query. Each case
in the normalized distribution includes the complete evaluator material needed
to score reports; full source-page evidence archives are not included.

Runtime contracts are in `src/drbenchpro/data/validation.py`; loading is in
`loader.py`, and typed interface definitions are in `schemas.py`.

## Licensing and attribution

Data redistribution terms have not been selected by the project owner. The
normalized archive is staged locally and has not been published under an assumed
license. This code's future license must not be treated as a blanket data license.

The benchmark is derived from Wikipedia page relationships/content and generated
queries/rubrics. Source/target article titles and edge relationships are retained.
Before public redistribution, supply the approved data license and the necessary
Wikipedia attribution/provenance information. Relevant upstream terms are
[Wikimedia Terms of Use](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use).
Downloaded Wikipedia dumps, model outputs and dependencies retain their own
applicable terms. A prebuilt snapshot/index is not bundled or newly licensed here.
