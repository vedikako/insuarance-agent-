from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai_agents.contracts.errors import AgentError


class ToolCallRecord(BaseModel):
    tool_name: str
    ok: bool
    summary: str


class ClauseHit(BaseModel):
    chunk_id: str
    section_path: str
    quote: str


class PolicyQARequest(BaseModel):
    session_id: str
    user_id: str
    role: Literal["CUSTOMER", "INSURANCE_COMPANY", "AUDITOR"]
    policy_id: str
    message: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class PolicyQAResult(BaseModel):
    agent: Literal["policy_qa"] = "policy_qa"
    status: Literal["ok", "error"]
    route: Literal["direct", "data"]
    reply: str
    citations: list[ClauseHit] = Field(default_factory=list)
    balance: dict[str, Any] | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    error: AgentError | None = None


class FraudFlag(BaseModel):
    code: str
    severity: Literal["medium", "high"]
    detail: str


class FraudReviewRequest(BaseModel):
    claim_id: str = Field(min_length=1)
    user_id: str
    role: Literal["INSURANCE_COMPANY", "AUDITOR"]


class FraudReviewResult(BaseModel):
    agent: Literal["fraud"] = "fraud"
    status: Literal["ok", "error"]
    risk: Literal["low", "medium", "high"]
    flags: list[FraudFlag] = Field(default_factory=list)
    summary: str
    error: AgentError | None = None
