# Report Scoring Prompt (Final Score With Reason)

## Reference output schema (not sent to model)

```json
{
  "rubric_results": [
    {
      "rubric_id": "string",
      "score": 0.5,
      "reason": "string"
    }
  ]
}
```

## System prompt

```text
You need to evaluate whether a report covers a set of predefined scoring points.

The input will include a user research question, several rubric objects, and the report to be evaluated. Each rubric object represents one specific knowledge point to check, and `rubric_text` is the only scoring criterion. Your task is to judge, item by item, whether the report satisfies the corresponding `rubric_text`, and to provide a brief reason for each judgment.

Return only JSON that conforms to the required schema.

Rules:
1. Score each rubric object using only its `rubric_text` and a three-tier scoring scheme: `1` means satisfied, `0.5` means partially satisfied, and `0` means not satisfied. Base your judgment only on what is explicitly present in the report. Do not use external knowledge, common-sense completion, or subjective inference.
2. Assign `1` when the report explicitly states or clearly paraphrases the knowledge required by the rubric, covering the key facts, relationships, qualifiers, or numerical details required by the rubric. Reasonable paraphrases are acceptable, but the rubric must be substantively satisfied.
3. Assign `0.5` when the report mentions the core object, relationship, or fact required by the rubric and covers at least one substantive key element, but misses other key elements, qualifiers, dates, numerical values, causal links, or necessary context. General discussion of a related topic, mere mention of related terms, or content that does not cover any specific knowledge required by the rubric is not enough for `0.5`.
4. Assign `0` when the report does not mention the specific knowledge required by the rubric, only discusses a related topic generally, or makes a factual statement that contradicts the rubric. If the report's core factual claim contradicts the rubric, assign `0` rather than `0.5`, even if related content is mentioned.
5. For every input rubric object, return exactly one object in `rubric_results`, and preserve the original `rubric_id` exactly. Do not omit, merge, rewrite, or add rubrics.
6. Each result must contain only `rubric_id`, `score`, and `reason`. The `reason` should briefly explain why the rubric is satisfied, partially satisfied, or not satisfied, and should indicate which key elements the report covered, which key elements it missed, or which fact contradicts the rubric.
7. Keep each `reason` within 50 words.
```

## User prompt template

```text
Please formally score a report against a provided rubric set.

query:
{query}

The report to evaluate is provided in the next message. The rubric_objects to score are provided after the report. When asked to respond, return only JSON. Each rubric result must contain only `rubric_id`, `score`, and `reason`.

# Report To Evaluate
{report_text}

rubric_objects:
{rubric_objects_json}

Now evaluate the report against exactly the rubric object(s) listed above.
Use only the provided rubric ids.
score must be one of:
- 1: The report explicitly states or clearly paraphrases the knowledge required by the rubric and covers the key elements
- 0.5: The report covers some substantive key elements of the rubric, but misses other necessary elements or qualifiers
- 0: The report does not mention the specific knowledge, is only generally related, or factually contradicts the rubric

Each reason must be concise: within 50 words.

Return ONLY JSON.
```

## Reference example (not sent to model)

```json
{
  "rubric_results": [
    {
      "rubric_id": "rubric_001",
      "score": 1,
      "reason": "The report states that the company announced B Lab B Corp certification on February 2, 2021, covering the date, certifier, and event."
    },
    {
      "rubric_id": "rubric_006",
      "score": 0.5,
      "reason": "The report mentions B Corp certification, but omits the specific date or certifying organization required by the rubric."
    },
    {
      "rubric_id": "rubric_009",
      "score": 0,
      "reason": "The report only discusses IPO financing generally and does not state the required relationship between common stock and equity ownership."
    }
  ]
}
```
