# Relation Generation Prompt

## Purpose

Generate one directional relation label and short grounded interpretation for one graph edge.

## Why this prompt exists in our benchmark

This prompt gives the graph a reusable semantic edge layer before query and rubric generation.

The goal is not to write a long explanation. The goal is to produce a compact relation signal that:

- is grounded in the source/target/anchor/context evidence
- is direction-aware
- is useful for later query summarization and rubric wording
- remains conservative when the evidence is weak

## Inputs

This prompt should be called on one edge at a time.

- `source_node`
- `target_node`
- `anchor_text`
- `source_context`
- optional `target_snippet`
- optional `source_neighbors`
- optional `target_neighbors`
- optional `existing_relation_hint`

## Output schema

```json
{
  "candidate_relation": "string",
  "relation_definition": "string",
  "directionality": "directed | symmetric | unclear",
  "confidence": 0.0,
  "evidence_or_rationale": "string",
  "alternative_relations": [
    "string"
  ],
  "rejection_notes": [
    "string"
  ]
}
```

## System prompt

```text
You are generating a compact semantic relation label for ONE graph edge.

Return ONLY JSON using the required schema.

Rules:
1. Infer the relation only from the provided source/target/anchor/context materials.
2. Prefer short, semantic, program-friendly labels such as "subsidiary_of", "develops_from", "acquired_by", "component_of", "trading_name", "challenge_to", or "other".
3. The label should be directional when direction matters.
4. Be conservative. If the edge evidence is weak or ambiguous, use a broad but still meaningful label rather than inventing a strong claim.
5. `relation_definition` should explain the inferred relation in one sentence grounded in the evidence.
6. `evidence_or_rationale` should briefly say which evidence supports the label.
7. Do not produce a report-style paragraph.
8. Do not merge information from other edges unless it appears in the provided optional neighbor hints.
```

## User prompt template

```text
Generate one relation object from the following edge evidence.

source_node:
{source_node}

target_node:
{target_node}

anchor_text:
{anchor_text}

source_context:
{source_context}

target_snippet:
{target_snippet}

optional_source_neighbors:
{source_neighbors}

optional_target_neighbors:
{target_neighbors}

optional_existing_relation_hint:
{existing_relation_hint}

Return ONLY JSON.
```

## Minimal example

```json
{
  "candidate_relation": "acquired_by",
  "relation_definition": "The source entity was acquired by the target entity according to the source context.",
  "directionality": "directed",
  "confidence": 0.93,
  "evidence_or_rationale": "The context explicitly states that the source was acquired by the target in a specific year.",
  "alternative_relations": [
    "subsidiary_of"
  ],
  "rejection_notes": [
    "Do not use subsidiary_of as the main label if the context is specifically about the acquisition event."
  ]
}
```

## Notes / failure modes

- If the label is too generic, later query and rubric generation lose semantic signal.
- If the label is too aggressive, the graph bakes in unsupported semantics.
- If the edge only shows co-mention, prefer a weak label or `other`.

## How it differs from DRB1 / DRB2 / ResearchRubrics where relevant

- This is graph-construction metadata, not reference-based scoring.
- It supports automated KG construction rather than expert rubric writing.
- It is intentionally lower-level than the later report-scoring prompts.
