from __future__ import annotations

from typing import Any, TypedDict


class EligibilityState(TypedDict, total=False):
    request: dict[str, Any]
    query: str
    critiqued: bool
    clauses: list[dict[str, Any]]
    draft: dict[str, Any]
    groundedness_score: int
    judge_rationale: str
    unsupported_statements: list[str]
    tool_calls: list[dict[str, Any]]
    result: dict[str, Any]
