"""Document agent.

Graph: plan → extract → validate → reextract once if a field is missing → finalize.
It does not score fraud and it does not decide eligibility.
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import DocumentAgentRequest, DocumentAgentResult, ExtractedDocument, ToolCallRecord
from app.ai_agents.document.graph import get_document_graph
from app.ai_agents.document.state import DocumentState

logger = logging.getLogger(__name__)


def _bad_attempt() -> DocumentAgentResult:
    return DocumentAgentResult.model_construct(
        status="error",
        attempt=1,
        package_status="needs_reupload",
        documents=[],
        missing_fields=[],
        kyc_match=False,
        kyc_reason="",
        needs_manual_review=True,
        error=AgentError(code="BAD_INPUT", message="Attempt must be 1 or 2.", retryable=False),
    )


async def run_document_agent(
    request: DocumentAgentRequest,
    *,
    reader: Any | None = None,
    llm: Any | None = None,
) -> DocumentAgentResult:
    if getattr(request, "attempt", None) not in (1, 2):
        return _bad_attempt()
    if request.attempt == 2 and request.revision is None:
        return DocumentAgentResult(
            status="error",
            attempt=2,
            package_status="needs_reupload",
            kyc_match=False,
            needs_manual_review=True,
            error=AgentError(code="BAD_INPUT", message="A revision is required on attempt 2.", retryable=False),
        )
    logger.info("document agent start claim_id=%s attempt=%s", request.claim_id, request.attempt)
    initial: DocumentState = {"request": request.model_dump(mode="json"), "reextract_done": False}
    final = await get_document_graph().ainvoke(
        initial,
        config={"configurable": {"reader": reader, "llm": llm}},
    )
    result = final.get("result") or {}
    documents = [ExtractedDocument.model_validate(item) for item in result.get("documents") or []]
    return DocumentAgentResult(
        status="ok",
        attempt=request.attempt,
        package_status=result.get("package_status") or "needs_reupload",
        documents=documents,
        missing_fields=list(result.get("missing_fields") or []),
        kyc_match=bool(result.get("kyc_match")),
        kyc_reason=str(result.get("kyc_reason") or ""),
        tool_calls=[ToolCallRecord.model_validate(item) for item in result.get("tool_calls") or []],
        needs_manual_review=bool(result.get("needs_manual_review")),
    )
