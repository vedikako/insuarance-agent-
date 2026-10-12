from __future__ import annotations

from typing import Any, TypedDict


class DocumentState(TypedDict, total=False):
    request: dict[str, Any]
    focus_fields: list[str]
    extracted: list[dict[str, Any]]
    reextract_done: bool
    needs_reextract: bool
    error_code: str
    error_message: str
    result: dict[str, Any]
