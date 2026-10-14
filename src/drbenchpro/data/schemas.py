"""Public typed interfaces; validation.py enforces runtime constraints."""
from typing import TypedDict, NotRequired

class Rubric(TypedDict):
    rubric_id: str
    rubric_text: str
    rubric_depth: int
    parent_rubric_id: str | None
    source_title: NotRequired[str]
    target_title: NotRequired[str]

class Query(TypedDict):
    case_id: str
    query: str

class Case(Query):
    rubrics: list[Rubric]
    domain: NotRequired[str]

class Report(TypedDict):
    case_id: str
    report: str
    system_id: NotRequired[str]
    status: NotRequired[str]

class Score(TypedDict):
    case_id: str
    rubric_id: str
    score: float
    system_id: NotRequired[str]
