# Subgraph Edge Rubric Batch Generation Prompt

## Purpose

For each source-target entity pair in a batch, generate an evidence-grounded rubric object based on Wikipedia page evidence.

## Usage Context

This is the batch generation version. It processes multiple source-target relationship records at once while preserving the rule that each relationship record corresponds to one rubric.

## Inputs

- `edge_rubric_inputs`
  - Each item includes:
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

## Reference output schema (not sent to model)

```json
{
  "rubrics": [
    {
      "rubric_id": "string",
      "rubric_text": "string"
    }
  ]
}
```

## System prompt

```text
You will receive a batch of relationship records, each made from a source entity and a target entity. Each record includes evidence from Wikipedia page summaries and relationship context. Your task is to write one verifiable knowledge-coverage requirement for each record, so that a later research report can be checked for whether it clearly covers the specific knowledge relationship expressed by that record.

For each relationship record, use only the materials provided by that record: source page summary, target page summary, and relation evidence sentence / paragraph. Do not use internal structural information such as node depth, path position, centrality, or subgraph topology.

Return ONLY JSON that follows the required schema.

Rules:
1. Generate exactly one rubric object for each provided `rubric_id`.
2. Each rubric must be faithful to that relationship record's own source/target/evidence materials, and must be written as a knowledge coverage requirement for report content, not as a writing-quality judgment.
3. The rubric must be based on the provided page summaries and relationship evidence. Do not invent claims beyond the summaries and relation evidence. If the evidence is weak, keep the rubric narrow and concrete. If the evidence sentence supports a clear relationship, prefer explicit relation coverage. A report may satisfy the same knowledge content through a clear paraphrase.
4. Use the exact key names from the output schema. Each rubric may return only `rubric_id` and `rubric_text`.
```

## User prompt template

```text
For each relationship record in the following list, generate one rubric object based on the provided Wikipedia page evidence.

edge_rubric_inputs:
{edge_rubric_inputs}

Return ONLY JSON.
```

## Reference example (not sent to model)

```json
{
  "rubrics": [
    {
      "rubric_id": "rubric_001_edge_001",
      "rubric_text": "The report explicitly states or clearly paraphrases that Bauhaus was founded by Walter Gropius."
    }
  ]
}
```
