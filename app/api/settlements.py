"""Company settlement advice.

POST /api/settlements calls run_settlement for one claim. It does not pay the
claim and it does not approve the review. Only the insurance company can call it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_agents.contracts.messages import SettlementRequest, SettlementResult
from app.ai_agents.settlement.port import SettlementDataPort
from app.ai_agents.settlement.runner import run_settlement
from app.api.policy_chat import Actor, get_actor
from app.db.session import get_db
from app.services.settlement_data import SqlSettlementData

router = APIRouter(prefix="/settlements", tags=["settlements"])


class SettlementIn(BaseModel):
    claim_id: str = Field(min_length=1)


def get_settlement_data(
    request: Request,
    db: AsyncSession | None = Depends(get_db),
) -> SettlementDataPort:
    port = getattr(request.app.state, "settlement_data", None)
    if port is not None:
        return port
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    return SqlSettlementData(db)


@router.post("", response_model=SettlementResult)
async def start_settlement(
    body: SettlementIn,
    actor: Actor = Depends(get_actor),
    data: SettlementDataPort = Depends(get_settlement_data),
) -> SettlementResult:
    if actor.role != "INSURANCE_COMPANY":
        raise HTTPException(status_code=403, detail="Only the insurance company can settle a claim")
    result = await run_settlement(
        SettlementRequest(claim_id=body.claim_id, user_id=actor.user_id, role="INSURANCE_COMPANY"),
        deps=data,
    )
    if result.error is not None and result.error.code == "BAD_INPUT":
        raise HTTPException(status_code=404, detail=result.error.message)
    return result
