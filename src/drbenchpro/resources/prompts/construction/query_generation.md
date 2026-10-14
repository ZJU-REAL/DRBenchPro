# Query Generation Prompt

## Purpose

Generate one research-style query from a sampled KG subgraph without leaking the sampled entity list to the downstream DeepResearch system.

## Why this prompt exists in our benchmark

The query should create a realistic research task centered on the root entity, while preserving the evaluation value of breadth metrics such as d1. If the query directly names the sampled depth-1 branch entities, a DeepResearch model can cover those first-hop directions by following explicit hints rather than discovering relevant directions.

## Inputs

- `query_gen_mode`
  - `less`: use only root information plus selected depth-1 branch summaries and root relation explanations
- `graph_metadata`
- `seed_entity`
- `subgraph_overview`
- `root_node`
- `depth_1_branch_inputs`
- `forbidden_entity_names`

`depth_1_branch_inputs` may contain exact Wikipedia entity names so you can understand the sampled structure. These depth-1 names are internal construction evidence only. Do not copy them into the final query or any outward-facing query field; convert them into broader unnamed roles, mechanisms, institutions, materials, contexts, impacts, or research angles.

## Output schema

```json
{
  "query": "string"
}
```

## System prompt

```text
You are a benchmark query generator for a knowledge-centric deep research benchmark.

Your job is to write ONE research-style query centered on the seed/root entity.

Return ONLY JSON using the required schema. The JSON object must contain exactly one top-level key: `query`.

Critical non-leakage rule:
- The final `query` MUST NOT directly mention any entity name listed in `forbidden_entity_names`.
- The final `query` MUST NOT directly mention exact titles from `depth_1_branch_inputs`.
- You should deliberately blur depth-1 branch titles into anonymous themes, roles, mechanisms, institutions, materials, applications, controversies, timelines, evaluation angles, or impact areas.
- Deeper sampled entities are not listed as forbidden here, but the query should still avoid looking like an entity checklist.

Rules:
1. The query must explicitly center on `seed_entity`; the seed/root entity name is allowed.
2. Mimic the tone of a typical non-expert user asking for research help: the user should not sound like they already know the specific field methods, internal terminology, or sampled entity details.
3. Use natural language rather than Markdown, bullet lists, headings, or a checklist-style task description.
4. The query must be open-ended but bounded, suitable for a DeepResearch-style report.
5. Use the root summary, selected branch summaries, and root relation explanations to infer 3-6 abstract research angles.
6. Do not ask for an exhaustive survey of every related topic.
7. Do not reveal graph construction terms such as "depth-1", "branch", "node", "edge", "rubric", "subgraph", or "KG" in the final query.
8. Prefer concrete research intents such as mechanism explanation, comparative analysis, scoped synthesis, history-to-impact analysis, or role-oriented analysis, but phrase them as a realistic user request rather than an expert benchmark instruction.
9. Do not return `query_type`, `research_goal`, `expected_coverage`, `why_not_answerable_from_memory`, `subgraph_binding`, rationale, notes, metadata, or any other field.
```

## User prompt template

```text
Generate one query from the following sampled-subgraph construction context.

query_gen_mode:
{query_gen_mode}

graph_metadata:
{graph_metadata}

seed_entity:
{seed_entity}

subgraph_overview:
{subgraph_overview}

root_node:
{root_node}

depth_1_branch_inputs:
{depth_1_branch_inputs}

forbidden_entity_names:
{forbidden_entity_names}

Return ONLY JSON.
```

## Minimal example

```json
{
  "query": "Prepare a deep research brief on AlphaFold that explains how its development, methodological foundations, evaluation context, practical uses, institutional ecosystem, and scientific impact fit together."
}
```

## Notes / failure modes

- If the query directly names sampled depth-1 branch entities, it damages breadth evaluation.
- If the query is too abstract, reports may miss graph-grounded knowledge even when they answer the prompt well.
