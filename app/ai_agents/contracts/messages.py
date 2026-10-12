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


class SettlementNote(BaseModel):
    code: str
    detail: str


class SettlementRequest(BaseModel):
    claim_id: str = Field(min_length=1)
    user_id: str
    role: Literal["INSURANCE_COMPANY", "AUDITOR"]


class SettlementResult(BaseModel):
    agent: Literal["settlement"] = "settlement"
    status: Literal["ok", "error"]
    decision: Literal["approve", "partial", "deny", "review"]
    payable_amount: float | None = None
    notes: list[SettlementNote] = Field(default_factory=list)
    summary: str
    error: AgentError | None = None


class DocumentRef(BaseModel):
    document_id: str
    type_code: Literal[
        "HOSPITAL_BILL",
        "DISCHARGE_SUMMARY",
        "AADHAAR",
        "PAN",
        "REPAIR_ESTIMATE",
        "FIR",
        "RC",
    ]
    mime: Literal["application/pdf", "image/png", "image/jpeg"]
    storage_uri: str
    filename: str


class DocumentRevision(BaseModel):
    reason: str
    focus_fields: list[str] = Field(default_factory=list)


class DocumentAgentRequest(BaseModel):
    claim_id: str
    claim_type: Literal["HEALTH", "MOTOR"]
    attempt: Literal[1, 2]
    policyholder_name: str
    policyholder_id_last4: str | None = None
    documents: list[DocumentRef] = Field(min_length=1)
    revision: DocumentRevision | None = None


class ExtractedDocument(BaseModel):
    document_id: str
    type_code: str
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    missing_fields: list[str] = Field(default_factory=list)


class DocumentAgentResult(BaseModel):
    agent: Literal["document"] = "document"
    status: Literal["ok", "error"]
    attempt: Literal[1, 2]
    package_status: Literal["accepted", "partial", "needs_reupload", "kyc_failed"]
    documents: list[ExtractedDocument] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    kyc_match: bool
    kyc_reason: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    needs_manual_review: bool = False
    error: AgentError | None = None


class LedgerSnapshot(BaseModel):
    sum_insured: float
    remaining_balance: float
    amount_already_paid: float


class Dispute(BaseModel):
    code: Literal["AMOUNT_CONFLICT", "DATE_CONFLICT", "IDENTITY_CONFLICT", "DIAGNOSIS_CONFLICT"]
    field: str
    fraud_value: str
    document_value: str
    detail: str


class EligibilityRevision(BaseModel):
    reason: str
    disputes: list[Dispute] = Field(default_factory=list)
    unsupported_statements: list[str] = Field(default_factory=list)


class EligibilityAgentRequest(BaseModel):
    claim_id: str
    policy_id: str
    product_document_id: str | None = None
    claim_type: Literal["HEALTH", "MOTOR"]
    claim_amount: float
    incident_date: str | None = None
    diagnosis: str = ""
    incident_summary: str = ""
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    ledger: LedgerSnapshot
    attempt: Literal[1, 2]
    revision: EligibilityRevision | None = None


class CitedClause(BaseModel):
    chunk_id: str
    section: str
    quote: str


class DisputeAnswer(BaseModel):
    code: str
    stance: Literal["sustain", "revise", "insufficient_evidence"]
    revised_payable_amount: float | None = None
    rationale: str


class EligibilityAgentResult(BaseModel):
    agent: Literal["eligibility"] = "eligibility"
    status: Literal["ok", "error"]
    attempt: Literal[1, 2]
    decision: Literal["eligible", "partially_eligible", "ineligible", "insufficient_evidence"]
    payable_amount: float
    currency: Literal["INR"] = "INR"
    groundedness_score: int
    judge_rationale: str = ""
    citations: list[CitedClause] = Field(default_factory=list)
    unsupported_statements: list[str] = Field(default_factory=list)
    answered_disputes: list[DisputeAnswer] = Field(default_factory=list)
    needs_manual_review: bool = False
    manual_review_reasons: list[str] = Field(default_factory=list)
    numeric_inconsistency: bool = False
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    error: AgentError | None = None


class FraudDocument(BaseModel):
    document_id: str
    type_code: str
    mime: str
    storage_uri: str
    fields: dict[str, Any] = Field(default_factory=dict)


class FraudPolicy(BaseModel):
    start_date: str
    end_date: str
    sum_insured: float
    holder_name: str


class FraudBehavior(BaseModel):
    recent_claims_30d: int
    total_claim_amount_90d: float
    prior_fraud_flags: int


class FraudRevision(BaseModel):
    reason: str
    flag: str
    question: str


class FraudAgentRequest(BaseModel):
    claim_id: str
    claim_type: Literal["HEALTH", "MOTOR"]
    claim_amount: float
    incident_date: str | None = None
    policy: FraudPolicy
    documents: list[FraudDocument] = Field(default_factory=list)
    behavior: FraudBehavior
    attempt: Literal[1, 2]
    revision: FraudRevision | None = None


class FraudAgentResult(BaseModel):
    agent: Literal["fraud"] = "fraud"
    status: Literal["ok", "error"]
    attempt: Literal[1, 2]
    score: float
    risk_level: Literal["MINIMAL", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
    flags: list[str] = Field(default_factory=list)
    critical: bool
    tools_used: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    explanation: str = ""
    disputes: list[Dispute] = Field(default_factory=list)
    needs_manual_review: bool = False
    manual_review_reasons: list[str] = Field(default_factory=list)
    error: AgentError | None = None


class ReviewRequest(BaseModel):
    claim_id: str = Field(min_length=1)
    user_id: str = ""
    role: Literal["INSURANCE_COMPANY", "AUDITOR"] = "INSURANCE_COMPANY"


class TraceEvent(BaseModel):
    id: str
    review_run_id: str
    claim_id: str
    seq: int
    from_agent: Literal["orchestrator", "document", "eligibility", "fraud", "system"]
    to_agent: Literal["orchestrator", "document", "eligibility", "fraud", "human"]
    kind: Literal["plan", "plan_rejected", "handoff", "result", "critique", "revision", "escalate", "error"]
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ReviewPackage(BaseModel):
    review_run_id: str
    claim_id: str
    run_status: Literal["running", "ready", "escalated", "failed"]
    recommended_action: Literal["approve_candidate", "reject_candidate", "manual_review"]
    reasons: list[str] = Field(default_factory=list)
    document: DocumentAgentResult | None = None
    eligibility: EligibilityAgentResult | None = None
    fraud: FraudAgentResult | None = None
    narrative: str = ""
    trace: list[TraceEvent] = Field(default_factory=list)
