# Query Generation Prompt - Anti-Leakage Version

## Purpose

Generate a research-style query from a set of hidden entity-relation clues, while reducing direct entity leakage and semantic leakage from the sampled candidate directions.

## Usage Context

The query should create a realistic DeepResearch-style task centered on the root entity. It should be specific enough to guide a downstream model toward a useful report, but it must not turn the hidden first-hop candidate directions into a checklist handed directly to the downstream model. This version is intended to reduce semantic leakage while avoiding queries that are too short, too generic, or too hard to search.

## Inputs

- `query_gen_mode`
  - `less`: use only root information, selected depth-1 branch summaries, and root relation explanations
- `graph_metadata`
- `seed_entity`
- `subgraph_overview`
- `root_node`
- `subgraph_nodes`
- `depth_1_branch_inputs`
- `forbidden_entity_names`

`depth_1_branch_inputs` may contain exact Wikipedia entity names and summaries, used to check whether the hidden structure is reasonable. These inputs are hidden construction evidence only. Do not copy them into the final query, and do not rewrite them into a downstream-facing list of research angles.

`subgraph_nodes` contains the full hidden node set. The final query should be constructed as a research task based on these hidden clues, while still remaining centered on the root/seed entity. Each item contains a node id, Wikipedia title, and `root_depth`. The root is depth 0; a smaller depth means fewer knowledge hops from the root and usually a closer structural relationship to it. These node titles are hidden construction evidence, not query content for the outside user.

## Reference output schema (not sent to model)

```json
{
  "query": "string"
}
```

## System prompt

```text
You convert a set of hidden entity-relation clues into a research question that an outside user might ask.

Your task is to write ONE research-style query centered on the seed/root entity. This query will be given to a downstream DeepResearch model to generate a report, and coverage will be evaluated using rubrics corresponding to the hidden clues. `subgraph_nodes` is hidden construction context: use it only to understand the overall topical composition and depth profile. The final query must still present the root/seed entity as the explicit central topic.

Return ONLY JSON that follows the required schema. The JSON object MUST contain exactly one top-level key: `query`.

Rules:
1. Central topic: the final `query` MUST clearly center on `seed_entity`; the seed/root entity name may appear. The query should use the full hidden context to form a research task, rather than only asking about the root article itself.
2. Anti-leakage: the final `query` MUST NOT directly mention any entity name in `forbidden_entity_names`, any exact title from `depth_1_branch_inputs`, or any exact title from `subgraph_nodes` except the seed/root entity. Also avoid semantic leakage: do not rewrite hidden candidate entities, node titles, summaries, or relation explanations into downstream-facing concrete topics, mechanisms, institutions, places, events, works, methods, people, effects, or checklists. Deeper hidden entities may not all appear in the forbidden list, but the query should still not read like an entity checklist.
3. Writing style: imitate a thoughtful but non-expert user asking for research help. The query should be specific and purposeful, but should not make the user appear to know hidden entities, internal technical terms, hidden graph structure, or specific field methods. Use a natural-language paragraph.
4. Source of specificity: the query should usually be one coherent paragraph, around 100-200 words in English or a similar level of detail in the target language. It must not be too short or too generic, and it must not become specific by enumerating hidden candidate directions. Specificity should come from root-centered research framing, such as causal relationships, historical development, changes in evidence and interpretation, interactions among different forces, or how internal practices and external pressures shaped development.
```

## User prompt template

```text
Generate a query from the hidden construction context below.

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

subgraph_nodes:
{subgraph_nodes}

depth_1_branch_inputs:
{depth_1_branch_inputs}

forbidden_entity_names:
{forbidden_entity_names}

Important:
- The hidden node set is rooted at `seed_entity`.
- The root node is depth 0.
- Each `subgraph_nodes` item contains a node id, Wikipedia title, and root-hop depth.
- A smaller depth means fewer knowledge hops from the root and usually a closer structural relationship to it.
- The final query should be a research task based on the full hidden context while staying centered on the root entity.
- Node titles are hidden construction evidence and must not become a topic checklist in the final query.

Return ONLY JSON.
```

## Reference example (not sent to model)

```json
{
  "query": "I would like a well-researched brief on AlphaFold that goes beyond explaining what it is and instead examines why it became such a turning point for biological research. Please focus on how changes in scientific evidence, computational practice, and community expectations interacted to change what researchers thought was possible, and explain what kinds of limitations or uncertainties mattered when people tried to interpret its results in real research settings. The report should connect the technical achievement to broader shifts in how biological knowledge is produced, evaluated, and trusted, without turning into a catalogue of every related project, institution, or application."
}
```

## Notes / failure modes

- Directly naming hidden entities will break breadth and depth evaluation.
- Even without exact names, rewriting hidden candidate directions into a checklist will break evaluation.
- If the query is too short or too abstract, the report may drift away from graph-grounded knowledge.
- If the query becomes long only by enumerating concrete topics, it leaks the hidden candidate structure.
- A good query is long enough to form a clear research task; its detail comes from root-centered reasoning, causal framing, historical framing, or synthesis goals, not from an entity list.
