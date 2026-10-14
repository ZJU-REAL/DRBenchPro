# Relation Generation Prompt (Batch)

## Purpose

Generate one directional relation label and short grounded interpretation for each edge in a provided edge list.

## Why this prompt exists in our benchmark

This prompt is the batch variant of relation generation. It reduces API calls by labeling a full graph edge set in one request while preserving edge-level outputs.

## Inputs

- `edge_evidence_list`
  - each item contains:
    - `edge_id`
    - `source_node`
    - `target_node`
    - `anchor_text`
    - `source_context`
    - optional `target_snippet`

## Output schema

```json
{
  "relations": [
    {
      "edge_id": "string",
      "candidate_relation": "string",
      "relation_definition": "string",
      "evidence_or_rationale": "string"
    }
  ]
}
```

## System prompt

```text
You are generating compact semantic relation labels for a batch of graph edges.

Return ONLY JSON using the required schema.

Rules:
1. You must return one relation object for every provided edge_id.
2. Infer each relation only from that edge's own source/target/anchor/context materials.
3. Prefer short, semantic, program-friendly labels such as "subsidiary_of", "develops_from", "acquired_by", "component_of", "trading_name", "challenge_to", or "other".
4. The label should be directional when direction matters.
5. Be conservative. If the edge evidence is weak or ambiguous, use a broad but still meaningful label rather than inventing a strong claim.
6. `relation_definition` should explain the inferred relation in one sentence grounded in the evidence.
7. `evidence_or_rationale` should briefly say which evidence supports the label.
8. Do not omit any edge_id. Do not merge multiple edges into one relation object.
9. Keep each edge independent even though they appear in one batch.
```

## User prompt template

```text
Generate one relation object for each edge in the following edge evidence list.

edge_evidence_list:
{edge_evidence_list}

Return ONLY JSON.
```

## Minimal example

```json
{
  "relations": [
    {
      "edge_id": "edge_001",
      "candidate_relation": "acquired_by",
      "relation_definition": "The source entity was acquired by the target entity according to the source context.",
      "evidence_or_rationale": "The context explicitly states that the source was acquired by the target."
    },
    {
      "edge_id": "edge_002",
      "candidate_relation": "other",
      "relation_definition": "The source and target are related in the context, but the exact relation is not explicit enough to assign a narrower label.",
      "evidence_or_rationale": "The context shows a link mention without a clear directional semantic relation."
    }
  ]
}
```
