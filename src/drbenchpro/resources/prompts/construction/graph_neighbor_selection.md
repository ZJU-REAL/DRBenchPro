# Graph Neighbor Selection Prompt

## Purpose

During graph construction, select the most semantically and logically central outgoing Wikipedia entities for one source node. Candidate links may come from the full source article, including both lead-section and body-section links.

## Inputs

The prompt is called on one source page at a time.

- `source_title`
- `source_depth`
- `source_summary`
- `target_neighbor_count`
- `candidate_neighbors`

Each candidate includes:

- `candidate_id`
- `wikipedia_title`
- `anchor_text`
- `section`
- `paragraph_id`
- `context_text`

## Reference output schema (not sent to model)

```json
{
  "selected_candidate_ids": [
    "cand_001"
  ]
}
```

## System prompt

```text
Given a source Wikipedia page and the candidate links from its full article, select the outgoing entities that are best suited as next-layer expansion topics. These entities should be central and directly related to the source topic, and they should provide valuable evidence entry points for the later research scope.

Return ONLY JSON that follows the required schema.

Rules:
1. Select from the full source-article candidate pool, including both lead-section and body-section candidates. You may choose only from the provided `candidate_neighbors`.
2. Choose the candidates that are most semantically central, most directly related to the source entity, and most useful for expanding understanding of it.
3. If the candidate count is at least `target_neighbor_count`, return exactly `target_neighbor_count` unique candidates.
4. Return only `selected_candidate_ids`.
```

## User prompt template

```text
Select the best outgoing neighbors for the following source node.

source_title:
{source_title}

source_depth:
{source_depth}

source_summary:
{source_summary}

target_neighbor_count:
{target_neighbor_count}

candidate_neighbors:
{candidate_neighbors}

Return ONLY JSON.
```

## Reference example (not sent to model)

```json
{
  "selected_candidate_ids": [
    "cand_002",
    "cand_005",
    "cand_001"
  ]
}
```
