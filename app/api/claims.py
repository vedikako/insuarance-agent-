"""Claim picker for the company review page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.policy_chat import Actor, get_actor
from app.db.session import get_db

router = APIRouter(prefix="/claims", tags=["claims"])


def _row(item: dict) -> dict:
    return {
        "id": str(item["id"]),
        "claim_number": item.get("claim_number") or "",
        "claim_type": item.get("claim_type") or "",
        "status": item.get("status") or "",
        "claim_amount": item.get("claim_amount"),
        "policy_number": item.get("policy_number") or "",
    }


@router.get("")
async def list_claims(
    request: Request,
    actor: Actor = Depends(get_actor),
    db: AsyncSession | None = Depends(get_db),
):
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
                SELECT id, claim_number, claim_type, status, claim_amount, policy_number
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
                SELECT id, claim_number, claim_type, status, claim_amount, policy_number
                FROM claims
                ORDER BY updated_at DESC
                LIMIT 100
                """
            )
        )
    return [
        {
            "id": str(row.id),
            "claim_number": row.claim_number,
            "claim_type": row.claim_type,
            "status": row.status,
            "claim_amount": float(row.claim_amount) if row.claim_amount is not None else None,
            "policy_number": row.policy_number,
        }
        for row in result
    ]
