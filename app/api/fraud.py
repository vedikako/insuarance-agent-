from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_agents.contracts.messages import FraudReviewRequest
from app.ai_agents.fraud.port import FraudDataPort
from app.ai_agents.fraud.runner import run_fraud_review
from app.api.policy_chat import Actor, get_actor
from app.db.session import get_db
from app.services.fraud_data import SqlFraudData

router = APIRouter(prefix="/fraud", tags=["fraud"])


class ReviewIn(BaseModel):
    claim_id: str = Field(min_length=1)


async def get_fraud_data(
    request: Request,
    db: AsyncSession | None = Depends(get_db),
) -> FraudDataPort:
    port = getattr(request.app.state, "fraud_data", None)
    if port is not None:
        return port
    if db is None:
        raise HTTPException(status_code=503, detail="Fraud data is not configured")
    return SqlFraudData(db)


@router.post("/reviews")
async def review_claim(
    body: ReviewIn,
    actor: Actor = Depends(get_actor),
    data: FraudDataPort = Depends(get_fraud_data),
):
    if actor.role not in {"INSURANCE_COMPANY", "AUDITOR"}:
        raise HTTPException(status_code=403, detail="This role cannot review claims")
    result = await run_fraud_review(
        FraudReviewRequest(claim_id=body.claim_id, user_id=actor.user_id, role=actor.role),
        deps=data,
    )
    if result.status == "error" and result.error is not None:
        if result.error.code == "BAD_INPUT":
            raise HTTPException(status_code=404, detail=result.error.message)
        raise HTTPException(status_code=503, detail=result.error.message)
    return result
