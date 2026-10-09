from __future__ import annotations

from typing import Any, TypedDict


class PolicyQAState(TypedDict, total=False):
    message: str
    history: list[dict[str, Any]]
    policy_id: str
    route: str
    tools_needed: list[str]
    supervisor_reason: str
    direct_reply: str
    data_context: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    reply: str
    cited_chunk_ids: list[str]
    error_code: str
    error_message: str
