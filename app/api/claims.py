"""Claims the customer files and the company decides.

The review recommendation is not stored here. Accept and Reject are the only
writes that change a claim's status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.policy_chat import Actor, get_actor
from app.db.session import get_db
from app.services.claim_files import DOCUMENT_TYPES, document_mime, safe_filename, save_upload

router = APIRouter(prefix="/claims", tags=["claims"])


class DecisionIn(BaseModel):
    choice: str = Field(pattern="^(accept|reject)$")
    payable_amount: float | None = None


def _row(item: dict) -> dict:
    amount = item.get("claim_amount")
    approved = item.get("approved_amount")
    return {
        "id": str(item["id"]),
        "claim_number": item.get("claim_number") or "",
        "claim_type": item.get("claim_type") or "",
        "status": item.get("status") or "",
        "claim_amount": float(amount) if amount is not None else None,
        "approved_amount": float(approved) if approved is not None else None,
        "policy_number": item.get("policy_number") or "",
        "description": item.get("description") or "",
    }


def _records(request: Request) -> dict | None:
    records = getattr(request.app.state, "claim_records", None)
    return records if isinstance(records, dict) else None


def _policies(request: Request) -> list[dict] | None:
    catalog = getattr(request.app.state, "policy_catalog", None)
    return list(catalog) if isinstance(catalog, list) else None


@router.get("")
async def list_claims(
    request: Request,
    actor: Actor = Depends(get_actor),
    db: AsyncSession | None = Depends(get_db),
):
    records = _records(request)
    if records is not None:
        rows = list(records.values())
        if actor.role == "CUSTOMER":
            rows = [item for item in rows if str(item.get("user_id")) == actor.user_id]
        return [_row(item) for item in rows]
    catalog = getattr(request.app.state, "claim_catalog", None)
    if catalog is not None:
        rows = list(catalog)
        if actor.role == "CUSTOMER":
            rows = [item for item in rows if str(item.get("user_id")) == actor.user_id]
        return [_row(item) for item in rows]
    if db is None:
        return []
    if actor.role == "CUSTOMER":
        result = await db.execute(
            text(
                """
                SELECT id, claim_number, claim_type, status, claim_amount, approved_amount,
                       policy_number, description
                FROM claims
                WHERE user_id = :user_id
                ORDER BY updated_at DESC
                LIMIT 100
                """
            ),
            {"user_id": actor.user_id},
        )
    else:
        result = await db.execute(
            text(
                """
                SELECT id, claim_number, claim_type, status, claim_amount, approved_amount,
                       policy_number, description
                FROM claims
                ORDER BY updated_at DESC
                LIMIT 100
                """
            )
        )
    return [_row(dict(row._mapping)) for row in result]


@router.post("/{claim_id}/decision")
async def decide_claim(
    claim_id: str,
    body: DecisionIn,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: AsyncSession | None = Depends(get_db),
):
    if actor.role != "INSURANCE_COMPANY":
        raise HTTPException(status_code=403, detail="Only the insurance company can decide a claim")
    if body.choice == "accept" and body.payable_amount is not None and body.payable_amount < 0:
        raise HTTPException(status_code=400, detail="The payable amount cannot be negative")
    records = _records(request)
    if records is not None:
        claim = records.get(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="Claim not found")
        _apply_choice(claim, body)
        return _row(claim)
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    found = await db.execute(
        text("SELECT id, claim_amount, status FROM claims WHERE id = :id"),
        {"id": claim_id},
    )
    current = found.first()
    if current is None:
        raise HTTPException(status_code=404, detail="Claim not found")
    status, approved, notes = _choice_values(body, current.claim_amount)
    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            """
            UPDATE claims
            SET status = :status,
                approved_amount = :approved_amount,
                adjuster_notes = :notes,
                updated_at = :now
            WHERE id = :id
            """
        ),
        {
            "status": status,
            "approved_amount": approved,
            "notes": notes,
            "now": now,
            "id": claim_id,
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO claim_status_history (
                id, claim_id, from_status, to_status, changed_by, reason, notes, created_at, updated_at
            )
            VALUES (
                :id, :claim_id, :from_status, :to_status, :changed_by, :reason, :notes, :now, :now
            )
            """
        ),
        {
            "id": str(uuid4()),
            "claim_id": claim_id,
            "from_status": current.status,
            "to_status": status,
            "changed_by": actor.user_id,
            "reason": notes,
            "notes": notes,
            "now": now,
        },
    )
    await db.commit()
    result = await db.execute(
        text(
            """
            SELECT id, claim_number, claim_type, status, claim_amount, approved_amount,
                   policy_number, description
            FROM claims
            WHERE id = :id
            """
        ),
        {"id": claim_id},
    )
    return _row(dict(result.first()._mapping))


@router.post("", status_code=201)
async def file_claim(
    request: Request,
    policy_id: str = Form(),
    claim_amount: float = Form(),
    description: str = Form(""),
    incident_date: str = Form(""),
    document_types: list[str] = Form(),
    files: list[UploadFile] = File(),
    actor: Actor = Depends(get_actor),
    db: AsyncSession | None = Depends(get_db),
):
    if actor.role != "CUSTOMER":
        raise HTTPException(status_code=403, detail="Only a customer can file a claim")
    if claim_amount <= 0:
        raise HTTPException(status_code=400, detail="The claim amount must be positive")
    if not files or len(files) != len(document_types):
        raise HTTPException(status_code=400, detail="Each document needs a type and a file")
    prepared = []
    for upload, type_code in zip(files, document_types):
        code = type_code.strip().upper()
        if code not in DOCUMENT_TYPES:
            raise HTTPException(status_code=400, detail="That document type is not accepted")
        filename = safe_filename(upload.filename, f"{code.lower()}.bin")
        mime = document_mime(upload.content_type, filename)
        if mime is None:
            raise HTTPException(status_code=400, detail="Upload a PDF, PNG, or JPEG")
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail="One of the files is empty")
        prepared.append({"type_code": code, "filename": filename, "mime": mime, "data": data})

    policy = await _owned_policy(request, actor, db, policy_id)
    if claim_amount > float(policy["sum_insured"] or 0):
        raise HTTPException(status_code=400, detail="The claim amount is above the sum insured")
    claim_id = str(uuid4())
    claim_number = f"CLM-{str(policy['policy_type'])[:3].upper()}-{claim_id.split('-')[0].upper()}"
    now = datetime.now(timezone.utc)
    incident = incident_date.strip() or None
    claim = {
        "id": claim_id,
        "user_id": actor.user_id,
        "policy_id": policy_id,
        "claim_number": claim_number,
        "claim_type": str(policy["policy_type"] or "HEALTH"),
        "status": "SUBMITTED",
        "claim_amount": claim_amount,
        "approved_amount": None,
        "policy_number": policy["policy_number"],
        "description": description.strip(),
        "incident_date": incident,
    }
    stored_docs = []
    records = _records(request)
    if records is not None:
        for item in prepared:
            stored_docs.append(
                {
                    "document_id": str(uuid4()),
                    "type_code": item["type_code"],
                    "filename": item["filename"],
                    "mime": item["mime"],
                }
            )
        claim["documents"] = stored_docs
        records[claim_id] = claim
        return {**_row(claim), "documents": stored_docs}

    if db is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    await db.execute(
        text(
            """
            INSERT INTO claims (
                id, user_id, policy_id, claim_number, policy_number, claim_type,
                claim_amount, description, incident_date, status, created_at, updated_at
            )
            VALUES (
                :id, :user_id, :policy_id, :claim_number, :policy_number, :claim_type,
                :claim_amount, :description, :incident_date, 'SUBMITTED', :now, :now
            )
            """
        ),
        {**claim, "now": now},
    )
    for item in prepared:
        document_id, path = save_upload(claim_id, item["filename"], item["data"])
        await db.execute(
            text(
                """
                INSERT INTO claim_documents (
                    id, claim_id, uploader_id, document_type_code, storage_path,
                    original_filename, content_type, ocr_status, kyc_match_status,
                    validation_status, requires_manual_review, created_at, updated_at
                )
                VALUES (
                    :id, :claim_id, :uploader_id, :type_code, :storage_path,
                    :filename, :mime, 'PENDING', 'NOT_APPLICABLE',
                    'PENDING', false, :now, :now
                )
                """
            ),
            {
                "id": document_id,
                "claim_id": claim_id,
                "uploader_id": actor.user_id,
                "type_code": item["type_code"],
                "storage_path": path,
                "filename": item["filename"],
                "mime": item["mime"],
                "now": now,
            },
        )
        stored_docs.append(
            {
                "document_id": document_id,
                "type_code": item["type_code"],
                "filename": item["filename"],
                "mime": item["mime"],
            }
        )
    await db.commit()
    return {**_row(claim), "documents": stored_docs}


def _choice_values(body: DecisionIn, claim_amount) -> tuple[str, float | None, str]:
    if body.choice == "reject":
        return "REJECTED", None, "Rejected by the insurer."
    payable = body.payable_amount if body.payable_amount is not None else float(claim_amount or 0)
    return "APPROVED", round(float(payable), 2), "Accepted by the insurer."


def _apply_choice(claim: dict, body: DecisionIn) -> None:
    status, approved, notes = _choice_values(body, claim.get("claim_amount"))
    claim["status"] = status
    claim["approved_amount"] = approved
    claim["adjuster_notes"] = notes


async def _owned_policy(request: Request, actor: Actor, db: AsyncSession | None, policy_id: str) -> dict:
    catalog = _policies(request)
    if catalog is not None:
        for item in catalog:
            if str(item.get("id")) == policy_id:
                if str(item.get("user_id")) != actor.user_id:
                    raise HTTPException(status_code=403, detail="You cannot file a claim on this policy")
                return item
        raise HTTPException(status_code=404, detail="Policy not found")
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    result = await db.execute(
        text(
            """
            SELECT id, user_id, policy_number, policy_type, status, sum_insured
            FROM policies
            WHERE id = :id
            """
        ),
        {"id": policy_id},
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    if str(row.user_id) != actor.user_id:
        raise HTTPException(status_code=403, detail="You cannot file a claim on this policy")
    return dict(row._mapping)
