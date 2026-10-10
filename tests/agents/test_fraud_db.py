"""Runs the fraud SQL against the configured database when Postgres is up."""

import asyncio

import pytest
from sqlalchemy import text

from app.ai_agents.fraud.rules import score_claim
from app.config import settings
from app.db.session import get_sessionmaker
from app.services.fraud_data import SqlFraudData

REQUIRED = {
    "claims": {"id", "claim_amount", "approved_amount", "policy_id"},
    "policies": {"id", "status", "sum_insured"},
    "policy_utilization_entries": {"policy_id", "amount_delta", "coverage_bucket"},
}


def _connection_refused(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        message = str(current).lower()
        if "connection refused" in message or "1225" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


async def _check_live_database() -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        columns = await session.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN ('claims', 'policies', 'policy_utilization_entries')
                """
            )
        )
        found: dict[str, set[str]] = {}
        for row in columns:
            found.setdefault(row.table_name, set()).add(row.column_name)
        missing = {
            table: sorted(names - found.get(table, set()))
            for table, names in REQUIRED.items()
            if names - found.get(table, set())
        }
        assert not missing, f"fraud query columns are missing: {missing}"

        data = SqlFraudData(session)
        sample = await session.execute(text("SELECT id::text AS id FROM claims LIMIT 1"))
        sample_row = sample.first()
        if sample_row is None:
            missing_claim = await data.load_claim("missing-claim")
            assert missing_claim is None
            return

        snapshot = await data.load_claim(sample_row.id)
        assert snapshot is not None
        assert set(snapshot) == {
            "claim_amount",
            "approved_amount",
            "policy_status",
            "sum_insured",
            "remaining_balance",
            "other_claim_amounts",
        }
        scored = score_claim(snapshot)
        assert scored["risk"] in {"low", "medium", "high"}
        absent = await data.load_claim("missing-claim")
        assert absent is None


def test_fraud_sql_against_configured_database():
    if not settings.DATABASE_URL:
        pytest.skip("DATABASE_URL is not configured")
    try:
        asyncio.run(_check_live_database())
    except Exception as exc:
        if _connection_refused(exc):
            pytest.skip("Postgres is not accepting connections on 127.0.0.1:5432")
        raise
