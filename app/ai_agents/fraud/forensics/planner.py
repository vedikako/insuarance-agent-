from __future__ import annotations

from typing import Any

from app.ai_agents.fraud.forensics.tools import FLAG_TOOL, early_claim


def planned_tools(request: dict[str, Any]) -> list[str]:
    documents = request.get("documents") or []
    types = {str(document.get("type_code") or "") for document in documents}
    mimes = {str(document.get("mime") or "") for document in documents}
    behavior = request.get("behavior") or {}
    names: list[str] = []
    if types & {"HOSPITAL_BILL", "REPAIR_ESTIMATE"}:
        names.append("arithmetic_gst")
    if documents:
        names.append("cross_document")
    if mimes & {"application/pdf", "image/png", "image/jpeg"}:
        names.append("image_forensics")
    if types & {"AADHAAR", "PAN"}:
        names.append("id_redaction")
    if (
        int(behavior.get("recent_claims_30d") or 0) >= 3
        or int(behavior.get("prior_fraud_flags") or 0) > 0
        or early_claim(request)
    ):
        names.append("behavioral")
    if "DISCHARGE_SUMMARY" in types:
        names.append("content_review")
    return names[:6]


def tool_for_flag(flag: str) -> str:
    return FLAG_TOOL.get(flag, "cross_document")
