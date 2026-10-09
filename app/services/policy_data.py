"""Data port the Policy Q&A tools call. No free SQL from the model."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.policy_retrieval import retrieve_clauses


class PolicyDataPort(Protocol):
    async def get_owner_id(self, policy_id: str) -> str | None: ...

    async def get_balance(self, policy_id: str) -> dict[str, Any]: ...

    async def list_utilization_entries(self, policy_id: str) -> list[dict[str, Any]]: ...

    async def get_policy_facts(self, policy_id: str) -> dict[str, Any]: ...

    async def retrieve_clauses(self, policy_id: str, query: str, k: int = 4) -> list[dict[str, Any]]: ...

    async def list_claims_for_policy(self, policy_id: str) -> list[dict[str, Any]]: ...


class SqlPolicyData:
    """Reads the InsureFlow tables. Amounts come from the utilization ledger."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owner_id(self, policy_id: str) -> str | None:
        result = await self.db.execute(
            text("SELECT user_id FROM policies WHERE id = :policy_id"),
            {"policy_id": policy_id},
        )
        row = result.first()
        return None if row is None else str(row.user_id)

    async def get_balance(self, policy_id: str) -> dict[str, Any]:
        result = await self.db.execute(
            text(
                """
                SELECT p.sum_insured,
                       COALESCE(SUM(e.amount_delta), 0) AS utilized
                FROM policies p
                LEFT JOIN policy_utilization_entries e
                  ON e.policy_id = p.id AND e.coverage_bucket = 'BASE'
                WHERE p.id = :policy_id
                GROUP BY p.sum_insured
                """
            ),
            {"policy_id": policy_id},
        )
        row = result.first()
        if row is None:
            return {}
        sum_insured = float(row.sum_insured)
        utilized = float(row.utilized)
        remaining = max(sum_insured - utilized, 0.0)
        return {
            "policy_id": policy_id,
            "coverage_bucket": "BASE",
            "sum_insured": sum_insured,
            "utilized": utilized,
            "remaining_balance": remaining,
            "currency": "INR",
        }

    async def list_utilization_entries(self, policy_id: str) -> list[dict[str, Any]]:
        result = await self.db.execute(
            text(
                """
                SELECT entry_type, amount_delta, reason_code, effective_at
                FROM policy_utilization_entries
                WHERE policy_id = :policy_id
                ORDER BY effective_at DESC
                LIMIT 20
                """
            ),
            {"policy_id": policy_id},
        )
        return [
            {
                "entry_type": row.entry_type,
                "amount_delta": float(row.amount_delta),
                "reason_code": row.reason_code,
                "effective_at": row.effective_at.isoformat() if row.effective_at else None,
            }
            for row in result
        ]

    async def get_policy_facts(self, policy_id: str) -> dict[str, Any]:
        result = await self.db.execute(
            text(
                """
                SELECT policy_number, status, start_date, end_date, sum_insured,
                       premium_amount, deductible, copay_percentage, insured_name
                FROM policies
                WHERE id = :policy_id
                """
            ),
            {"policy_id": policy_id},
        )
        row = result.first()
        if row is None:
            return {}
        return {
            "policy_number": row.policy_number,
            "status": row.status,
            "start_date": row.start_date.isoformat() if row.start_date else None,
            "end_date": row.end_date.isoformat() if row.end_date else None,
            "sum_insured": float(row.sum_insured) if row.sum_insured is not None else None,
            "premium_amount": float(row.premium_amount) if row.premium_amount is not None else None,
            "deductible": float(row.deductible) if row.deductible is not None else None,
            "copay_percentage": float(row.copay_percentage) if row.copay_percentage is not None else None,
            "insured_name": row.insured_name,
        }

    async def retrieve_clauses(self, policy_id: str, query: str, k: int = 4) -> list[dict[str, Any]]:
        return await retrieve_clauses(policy_id, query, self.db, k=k)

    async def list_claims_for_policy(self, policy_id: str) -> list[dict[str, Any]]:
        result = await self.db.execute(
            text(
                """
                SELECT claim_number, status, claim_amount, approved_amount
                FROM claims
                WHERE policy_id = :policy_id
                ORDER BY updated_at DESC
                LIMIT 20
                """
            ),
            {"policy_id": policy_id},
        )
        return [
            {
                "claim_number": row.claim_number,
                "status": row.status,
                "claim_amount": float(row.claim_amount) if row.claim_amount is not None else None,
                "approved_amount": float(row.approved_amount) if row.approved_amount is not None else None,
            }
            for row in result
        ]
