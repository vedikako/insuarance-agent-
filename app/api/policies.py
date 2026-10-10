from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.policy_chat import Actor, get_actor
from app.db.session import get_db

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("")
async def list_policies(
    actor: Actor = Depends(get_actor),
    db: AsyncSession | None = Depends(get_db),
):
    if db is None:
        return []
    if actor.role == "CUSTOMER":
        result = await db.execute(
            text(
                """
                SELECT id, policy_number, policy_type, status, sum_insured
                FROM policies
                WHERE user_id = :user_id
                ORDER BY policy_number
                """
            ),
            {"user_id": actor.user_id},
        )
    else:
        result = await db.execute(
            text(
                """
                SELECT id, policy_number, policy_type, status, sum_insured
                FROM policies
                ORDER BY policy_number
                LIMIT 100
                """
            )
        )
    return [
        {
            "id": str(row.id),
            "policy_number": row.policy_number,
            "policy_type": row.policy_type,
            "status": row.status,
            "sum_insured": float(row.sum_insured) if row.sum_insured is not None else None,
        }
        for row in result
    ]
