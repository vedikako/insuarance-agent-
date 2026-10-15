"""Claim snapshot for the settlement rules. Amounts come from the ledger."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SqlSettlementData:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def load_claim(self, claim_id: str) -> dict[str, Any] | None:
        result = await self.db.execute(
            text(
                """
                SELECT c.claim_amount,
                       p.status AS policy_status,
                       p.deductible,
                       p.copay_percentage,
                       p.sum_insured,
                       COALESCE(SUM(e.amount_delta), 0) AS utilized
                FROM claims c
                JOIN policies p ON p.id = c.policy_id
                LEFT JOIN policy_utilization_entries e
                  ON e.policy_id = p.id AND e.coverage_bucket = 'BASE'
                WHERE c.id = :claim_id
                GROUP BY c.claim_amount, p.status, p.deductible, p.copay_percentage, p.sum_insured
                """
            ),
            {"claim_id": claim_id},
        )
        row = result.first()
        if row is None:
            return None
        sum_insured = float(row.sum_insured or 0)
        utilized = float(row.utilized or 0)
        return {
            "claim_amount": float(row.claim_amount),
            "policy_status": row.policy_status,
            "deductible": float(row.deductible or 0),
            "copay_percentage": float(row.copay_percentage or 0),
            "remaining_balance": max(sum_insured - utilized, 0.0),
        }
