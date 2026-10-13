"""Company claim review.

POST /api/reviews loads one claim and runs the review. Document and eligibility
are called from that run. The endpoint does not approve or reject the claim,
and it does not call policy Q&A. Auditors cannot start a review.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_agents.contracts.messages import ReviewPackage, ReviewRequest
from app.ai_agents.orchestrator.runner import run_review
from app.api.policy_chat import Actor, get_actor
from app.db.session import get_db

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewIn(BaseModel):
    claim_id: str = Field(min_length=1)


@router.post("", response_model=ReviewPackage)
async def start_review(
    body: ReviewIn,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: AsyncSession | None = Depends(get_db),
) -> ReviewPackage:
    if actor.role != "INSURANCE_COMPANY":
        raise HTTPException(status_code=403, detail="Only the insurance company can start a review")
    runner = getattr(request.app.state, "review_runner", None) or run_review
    if db is None and getattr(request.app.state, "review_runner", None) is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    package = await runner(
        ReviewRequest(claim_id=body.claim_id, user_id=actor.user_id, role="INSURANCE_COMPANY"),
        db,
    )
    if package.run_status == "failed" and package.narrative == "Claim not found.":
        raise HTTPException(status_code=404, detail="Claim not found")
    return package
