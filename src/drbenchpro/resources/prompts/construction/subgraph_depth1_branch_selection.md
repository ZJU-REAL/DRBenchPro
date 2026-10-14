# Depth-1 Branch Subgraph Selection Prompt

## Purpose

Under one root entity, select several groups of first-hop Wikipedia branches so that each group can become a natural deep-research subgraph.

Each selected group should be broad enough to support a systematic DeepResearch-style report, while avoiding wasting near-duplicate first-hop branches on the same research angle.

## Inputs

- `seed_entity`
- `root_summary`
- `target_subgraphs`
- `depth_1_branches_per_subgraph`
- `candidate_count`
- `depth_1_candidates`

Each candidate includes:

- `candidate_id`
- `node_id`
- `wikipedia_title`
- `summary`
- `root_relation_explanation`
- `root_relation`
  - `edge_id`
  - `relation_label`
  - `relation_explanation`
  - `context_text`
- `direct_children`
- `descendant_count`

`root_relation_explanation` is the preferred concise explanation of the relationship between the root entity and this depth-1 branch. It is generated during root first-hop graph construction. Use `root_relation.context_text` as fallback evidence only when that explanation is missing or too vague.

## Reference output schema (not sent to model)

```json
{
  "subgraphs": [
    {
      "selected_candidate_ids": [
        "d1_001",
        "d1_002"
      ]
    }
  ]
}
```

## System prompt

```text
You are selecting and combining candidate directions that are directly connected to a core Wikipedia topic, forming several subtopic sets suitable for deep research. Each set should feel like a natural research scope: its internal directions should be related and complementary, able to support later research-question framing, evidence organization, and evaluation of whether a report covers key knowledge relationships.

Return ONLY JSON that follows the required schema.

Rules:
1. When the candidate pool allows it, construct exactly `target_subgraphs` groups; when the candidate pool allows it, each group should contain exactly `depth_1_branches_per_subgraph` unique candidates. Choose only from the provided depth-1 candidate list.
2. Each group should form a coherent research scope: the selected first-hop branches should have semantic, historical, institutional, causal, comparative, or conceptual connections, and should be able to support a natural deep-research report. Do not simply choose the first candidates. Prefer branches whose descendant tree is more likely to provide effective supporting evidence.
3. Branches within a group should be complementary. Avoid homogeneous branches, near duplicates, or branches that can be represented by the same angle. Unless the seed topic naturally connects them, do not combine unrelated incidental mentions.
4. Different groups should overlap as little as possible. If overlap is unavoidable, prefer reusing candidates that have the most direct relationship to the seed/root, can connect multiple research angles, and whose descendant tree is more likely to provide effective supporting evidence.
5. Use `selected_candidate_ids` as the canonical output.
```

## User prompt template

```text
Select depth-1 branch groups for subgraph construction.

seed_entity:
{seed_entity}

root_summary:
{root_summary}

target_subgraphs:
{target_subgraphs}

depth_1_branches_per_subgraph:
{depth_1_branches_per_subgraph}

candidate_count:
{candidate_count}

depth_1_candidates:
{depth_1_candidates}

Return ONLY JSON.
```

## Reference example (not sent to model)

```json
{
  "subgraphs": [
    {
      "selected_candidate_ids": [
        "d1_002",
        "d1_005",
        "d1_007",
        "d1_009",
        "d1_011",
        "d1_014",
        "d1_016",
        "d1_018",
        "d1_020",
        "d1_023"
      ]
    }
  ]
}
```
