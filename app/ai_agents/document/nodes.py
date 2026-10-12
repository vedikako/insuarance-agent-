from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.ai_agents.contracts.messages import ToolCallRecord
from app.ai_agents.document.fields import missing_fields
from app.ai_agents.document.rules import classify_package, kyc_failure
from app.ai_agents.document.state import DocumentState
from app.ai_agents.document.tools import extract_document
from app.services.review_inputs import read_document_bytes

logger = logging.getLogger(__name__)


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


def _request(state: DocumentState) -> dict[str, Any]:
    return state.get("request") or {}


async def plan_node(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
    request = _request(state)
    focus = list(state.get("focus_fields") or [])
    revision = request.get("revision") or {}
    if not focus and revision:
        focus = list(revision.get("focus_fields") or [])
    logger.info("document plan claim_id=%s focus=%s", request.get("claim_id"), focus)
    return {"focus_fields": focus, "extracted": [], "needs_reextract": False}


async def extract_node(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _configurable(config)
    reader = deps.get("reader") or read_document_bytes
    llm = deps.get("llm")
    request = _request(state)
    focus = state.get("focus_fields") or []
    found = []
    for document in request.get("documents") or []:
        type_code = document["type_code"]
        try:
            data = await reader(document["storage_uri"])
        except Exception:
            logger.exception("document read failed uri=%s", document.get("storage_uri"))
            found.append(
                {
                    "document_id": document["document_id"],
                    "type_code": type_code,
                    "fields": {},
                    "confidence": 0.0,
                    "missing_fields": missing_fields(type_code, {}),
                    "read_ok": False,
                }
            )
            continue
        fields, confidence = await extract_document(
            data,
            type_code=type_code,
            focus_fields=focus or None,
            llm=llm,
        )
        found.append(
            {
                "document_id": document["document_id"],
                "type_code": type_code,
                "fields": fields,
                "confidence": confidence,
                "missing_fields": missing_fields(type_code, fields),
                "read_ok": True,
            }
        )
    return {"extracted": found}


async def validate_node(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
    request = _request(state)
    documents = state.get("extracted") or []
    attempt = int(request.get("attempt") or 1)
    kyc_reason = kyc_failure(
        str(request.get("policyholder_name") or ""),
        request.get("policyholder_id_last4"),
        documents,
    )
    weak = []
    for document in documents:
        if document.get("missing_fields") or float(document.get("confidence") or 0) < 0.60:
            weak.append(document["document_id"])
    needs = bool(weak) and not kyc_reason and attempt == 1 and not state.get("reextract_done")
    if needs:
        focus = []
        for document in documents:
            focus.extend(document.get("missing_fields") or [])
        return {"needs_reextract": True, "focus_fields": list(dict.fromkeys(focus))}
    return {"needs_reextract": False}


def route_after_validate(state: DocumentState) -> str:
    if state.get("needs_reextract"):
        return "reextract"
    return "finalize"


async def reextract_node(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
    logger.info("document reextract focus=%s", state.get("focus_fields"))
    return {"reextract_done": True, "needs_reextract": False}


async def finalize_node(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
    request = _request(state)
    documents = state.get("extracted") or []
    uploaded = {str(item.get("type_code")) for item in request.get("documents") or []}
    classified = classify_package(
        claim_type=str(request.get("claim_type") or "HEALTH"),
        policyholder_name=str(request.get("policyholder_name") or ""),
        policyholder_id_last4=request.get("policyholder_id_last4"),
        uploaded_types=uploaded,
        documents=documents,
    )
    tool_calls = [
        ToolCallRecord(
            tool_name="extract_document",
            ok=bool(document.get("read_ok")),
            summary=f"Extracted {document.get('type_code')} with confidence {document.get('confidence')}.",
        ).model_dump()
        for document in documents
    ]
    if state.get("reextract_done"):
        tool_calls.append(
            ToolCallRecord(
                tool_name="reextract",
                ok=True,
                summary="Re-read the documents once for the missing fields.",
            ).model_dump()
        )
    return {
        "result": {
            "attempt": request.get("attempt") or 1,
            "documents": [
                {
                    "document_id": document["document_id"],
                    "type_code": document["type_code"],
                    "fields": document.get("fields") or {},
                    "confidence": document.get("confidence") or 0,
                    "missing_fields": document.get("missing_fields") or [],
                }
                for document in documents
            ],
            "tool_calls": tool_calls,
            **classified,
        }
    }
