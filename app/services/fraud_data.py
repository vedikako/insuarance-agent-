"""Reads one claim snapshot for the fraud rules. No free SQL from the model."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def claim_uuid(value: str) -> str | None:
    """Claim ids are UUIDs. Anything else is not a stored claim."""
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def build_claim_snapshot(
    *,
    claim_amount: Any,
    approved_amount: Any,
    policy_status: Any,
    sum_insured: Any,
    utilized: Any,
    other_claim_amounts: list[Any],
) -> dict[str, Any]:
    sum_value = None if sum_insured is None else float(sum_insured)
    if sum_value is None:
        remaining = None
    else:
        remaining = max(sum_value - float(utilized or 0), 0.0)
    return {
        "claim_amount": None if claim_amount is None else float(claim_amount),
        "approved_amount": None if approved_amount is None else float(approved_amount),
        "policy_status": policy_status,
        "sum_insured": sum_value,
        "remaining_balance": remaining,
        "other_claim_amounts": [float(amount) for amount in other_claim_amounts if amount is not None],
    }


class SqlFraudData:
    """Loads claim, policy, ledger, and sibling claim amounts."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def load_claim(self, claim_id: str) -> dict[str, Any] | None:
        claim_id = claim_uuid(claim_id) or ""
        if not claim_id:
            return None
        result = await self.db.execute(
            text(
                """
                SELECT c.claim_amount, c.approved_amount, c.policy_id,
                       p.status AS policy_status, p.sum_insured
                FROM claims c
                JOIN policies p ON p.id = c.policy_id
                WHERE c.id = :claim_id
                """
            ),
            {"claim_id": claim_id},
        )
        row = result.first()
        if row is None:
            return None
        policy_id = str(row.policy_id)
        balance = await self.db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount_delta), 0) AS utilized
                FROM policy_utilization_entries
                WHERE policy_id = :policy_id AND coverage_bucket = 'BASE'
                """
            ),
            {"policy_id": policy_id},
        )
        utilized = balance.scalar()
        others = await self.db.execute(
            text(
                """
                SELECT claim_amount
                FROM claims
                WHERE policy_id = :policy_id
                  AND id <> :claim_id
                  AND UPPER(status::text) NOT IN ('REJECTED', 'CANCELLED', 'CANCELED')
                """
            ),
            {"policy_id": policy_id, "claim_id": claim_id},
        )
        return build_claim_snapshot(
            claim_amount=row.claim_amount,
            approved_amount=row.approved_amount,
            policy_status=row.policy_status,
            sum_insured=row.sum_insured,
            utilized=utilized,
            other_claim_amounts=[item.claim_amount for item in others],
        )
