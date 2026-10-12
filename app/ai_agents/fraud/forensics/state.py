from __future__ import annotations

from typing import Any, TypedDict


class ForensicsState(TypedDict, total=False):
    request: dict[str, Any]
    queue: list[str]
    used: list[str]
    observations: list[dict[str, Any]]
    next_tool: str
    done: bool
    result: dict[str, Any]
