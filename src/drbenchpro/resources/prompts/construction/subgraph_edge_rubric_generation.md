# Subgraph Edge Rubric Generation Prompt

## Purpose

Generate one edge-grounded rubric object from Wikipedia evidence for one source-target entity pair.

## Why this prompt exists in our benchmark

At this stage, the subgraph has already been sampled. The rubric generator should **not** reason from graph position, path depth, or broader topology.

Instead, each rubric should be grounded only in:

- the source entity's Wikipedia information
- the target entity's Wikipedia information
- the specific Wikipedia sentence or paragraph used as relation evidence

## Inputs

This prompt should be called on one edge at a time.

- `rubric_id`
- `source_title`
- `target_title`
- `source_page_summary`
- `target_page_summary`
- `relation_evidence_sentence`
- `relation_evidence_paragraph`
- `relation_evidence_source`
- `relation_evidence_anchor_text`
- `evidence_confidence`

## Output schema

```json
{
  "rubric_id": "string",
  "rubric_text": "string"
}
```

## System prompt

```text
You are generating one benchmark rubric from one source-target entity pair.

Use ONLY the provided Wikipedia-derived materials:
- source page summary
- target page summary
- relation evidence sentence / paragraph

Do NOT use graph depth, path position, centrality, or subgraph topology.

Return ONLY JSON using the required schema.

Rules:
1. Produce exactly one rubric for the provided entity pair.
2. The rubric must be a report-facing knowledge coverage requirement, not a writing-quality judgment.
3. Ground the rubric in the provided Wikipedia evidence. Do not invent claims that go beyond the summaries and relation evidence.
4. If the relation evidence is weak, keep the rubric narrow and concrete rather than broad or speculative.
5. Prefer explicit relation coverage when the evidence sentence supports a clear relation.
6. Allow clear paraphrase when the knowledge content is preserved.
7. Return only `rubric_id` and `rubric_text`; do not return edge ids, relation explanations, verification hints, confidence scores, source/target titles, or any other fields.
8. If uncertain, keep the rubric narrow and evidence-grounded rather than adding auxiliary explanation fields.
9. Do not mention graph depth, path position, "root", "leaf", or any structural graph language.
```

## User prompt template

```text
Generate one rubric object from the following Wikipedia-grounded edge evidence.

rubric_id:
{rubric_id}

source_title:
{source_title}

target_title:
{target_title}

source_page_summary:
{source_page_summary}

target_page_summary:
{target_page_summary}

relation_evidence_sentence:
{relation_evidence_sentence}

relation_evidence_paragraph:
{relation_evidence_paragraph}

relation_evidence_source:
{relation_evidence_source}

relation_evidence_anchor_text:
{relation_evidence_anchor_text}

evidence_confidence:
{evidence_confidence}

Return ONLY JSON.
```
