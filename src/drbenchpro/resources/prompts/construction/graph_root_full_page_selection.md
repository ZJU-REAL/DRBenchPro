# Graph Root Full-Page Selection Prompt

## Purpose

Select first-hop Wikipedia entities from links found in the full Wikipedia article for the root node. Candidate links include both lead-section and body-section links. The goal is to create broad, composable research directions for later deep-research query construction.

For each selected first-hop entity, also explain its direct relationship to the root entity using the provided anchor text and paragraph context. This explanation will be saved on the graph edge as `relation_explanation`.

## Inputs

- `root_title`
- `root_summary`
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
  ],
  "selected_neighbors": [
    {
      "candidate_id": "cand_001",
      "relation_explanation": "string"
    }
  ]
}
```

## System prompt

```text
You are selecting a set of directly related first-hop entities from the candidate links found in the full page of a core Wikipedia topic. These entities will become the main directions for later deep-research tasks, so you should balance direct relationship to the core topic, aspect coverage, and later composability.

Return ONLY JSON that follows the required schema. The JSON object MUST contain exactly two top-level keys: `selected_candidate_ids` and `selected_neighbors`.

Rules:
1. Candidate scope and count: choose entities only from the provided `candidate_neighbors`. The selection scope is the full-article candidate pool, including both lead-section and body-section candidates. If there are at least `target_neighbor_count` available unique candidates, return `target_neighbor_count` unique candidates. Return fewer only when the candidate list itself contains fewer available unique candidates than the target.
2. Selection objective: choose candidates that best support future root-centered deep-research tasks. Value here means research usefulness, aspect coverage, and composability for broad knowledge-coverage evaluation.
3. Coverage, combination, and duplicate filtering: prefer candidates that cover different research aspects of the root, and avoid concentrating on one narrow direction. Exclude highly similar titles, aliases, repeated directions, and obvious-common-knowledge or low-information candidates such as incidental mentions, dates, generic list pages, or weak parenthetical references. Form a differentiated branch set suitable for systematic DeepResearch.
4. Output structure: put the canonical selection in `selected_candidate_ids`. For each id in `selected_candidate_ids`, include exactly one object with the same `candidate_id` in `selected_neighbors`; each selected neighbor object MUST contain only `candidate_id` and `relation_explanation`.
5. Relationship explanation: `relation_explanation` MUST be one concise sentence explaining the relationship between the root and the selected entity, and it MUST be based only on that candidate's `anchor_text` and `context_text`.
```

## User prompt template

```text
Select first-hop root expansion entities for the following Wikipedia root.

root_title:
{root_title}

root_summary:
{root_summary}

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
    "cand_001",
    "cand_002",
    "cand_007",
    "cand_011"
  ],
  "selected_neighbors": [
    {
      "candidate_id": "cand_001",
      "relation_explanation": "The root article presents this entity as a founder or origin-related participant in the root topic."
    },
    {
      "candidate_id": "cand_002",
      "relation_explanation": "The root article links this entity as a parent institution or organizational context for the root topic."
    },
    {
      "candidate_id": "cand_007",
      "relation_explanation": "The root article links this entity in the context of regulation or public controversy around the root topic."
    },
    {
      "candidate_id": "cand_011",
      "relation_explanation": "The root article links this entity as a core technology or method related to the root topic."
    }
  ]
}
```
