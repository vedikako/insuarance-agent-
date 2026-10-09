from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AgentError(BaseModel):
    code: Literal["TIMEOUT", "MODEL_UNAVAILABLE", "TOOL_FAILED", "BAD_INPUT"]
    message: str
    retryable: bool
