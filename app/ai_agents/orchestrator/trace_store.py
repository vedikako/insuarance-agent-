"""One agent_traces row per event. Workers do not write this store."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_agents.contracts.messages import ReviewPackage, TraceEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore(Protocol):
    async def find_running(self, claim_id: str) -> str | None: ...

    async def start(self, claim_id: str, review_run_id: str) -> str: ...

    async def append(self, event: TraceEvent) -> TraceEvent: ...

    async def events(self, review_run_id: str) -> list[TraceEvent]: ...

    async def finish(self, claim_id: str, review_run_id: str, package: ReviewPackage) -> None: ...

    async def running_package(self, claim_id: str, review_run_id: str) -> ReviewPackage: ...


class MemoryTraceStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._seqs: dict[str, int] = {}
        self._events: dict[str, list[TraceEvent]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._by_id: dict[str, dict[str, Any]] = {}

    async def find_running(self, claim_id: str) -> str | None:
        run = self._runs.get(claim_id)
        if run and run["status"] == "running":
            return str(run["id"])
        return None

    async def start(self, claim_id: str, review_run_id: str) -> str:
        async with self._lock:
            existing = self._runs.get(claim_id)
            if existing and existing["status"] == "running":
                return str(existing["id"])
            record = {"id": review_run_id, "status": "running", "package": None}
            self._runs[claim_id] = record
            self._by_id[review_run_id] = {"claim_id": claim_id, **record}
            return review_run_id

    async def append(self, event: TraceEvent) -> TraceEvent:
        async with self._lock:
            seq = self._seqs.get(event.review_run_id, 0) + 1
            self._seqs[event.review_run_id] = seq
            stored = event.model_copy(update={"seq": seq})
            self._events.setdefault(event.review_run_id, []).append(stored)
            return stored

    async def events(self, review_run_id: str) -> list[TraceEvent]:
        return list(self._events.get(review_run_id, []))

    async def finish(self, claim_id: str, review_run_id: str, package: ReviewPackage) -> None:
        async with self._lock:
            record = {"id": review_run_id, "status": package.run_status, "package": package}
            self._runs[claim_id] = record
            self._by_id[review_run_id] = {"claim_id": claim_id, **record}

    async def running_package(self, claim_id: str, review_run_id: str) -> ReviewPackage:
        record = self._by_id.get(review_run_id) or {}
        saved = record.get("package")
        if isinstance(saved, ReviewPackage):
            return saved
        return ReviewPackage(
            review_run_id=review_run_id,
            claim_id=claim_id,
            run_status="running",
            recommended_action="manual_review",
            reasons=["REVIEW_RUNNING"],
            narrative="",
            trace=await self.events(review_run_id),
        )


class SqlTraceStore:
    """Writes agent_traces and review_runs. Claim status is not updated here."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._lock = asyncio.Lock()

    async def find_running(self, claim_id: str) -> str | None:
        result = await self.db.execute(
            text(
                """
                SELECT id FROM review_runs
                WHERE claim_id = :claim_id AND run_status = 'running'
                LIMIT 1
                """
            ),
            {"claim_id": claim_id},
        )
        row = result.first()
        return None if row is None else str(row.id)

    async def start(self, claim_id: str, review_run_id: str) -> str:
        existing = await self.find_running(claim_id)
        if existing:
            return existing
        now = datetime.now(timezone.utc)
        await self.db.execute(
            text(
                """
                INSERT INTO review_runs (id, claim_id, run_status, package, created_at, updated_at)
                VALUES (:id, :claim_id, 'running', NULL, :now, :now)
                """
            ),
            {"id": review_run_id, "claim_id": claim_id, "now": now},
        )
        await self.db.commit()
        return review_run_id

    async def append(self, event: TraceEvent) -> TraceEvent:
        async with self._lock:
            current = await self.db.execute(
                text("SELECT COALESCE(MAX(seq), 0) AS seq FROM agent_traces WHERE review_run_id = :run_id"),
                {"run_id": event.review_run_id},
            )
            seq = int(current.scalar() or 0) + 1
            stored = event.model_copy(update={"seq": seq})
            await self.db.execute(
                text(
                    """
                    INSERT INTO agent_traces (
                        id, review_run_id, claim_id, seq, from_agent, to_agent, kind, payload, created_at
                    )
                    VALUES (
                        :id, :review_run_id, :claim_id, :seq, :from_agent, :to_agent, :kind,
                        CAST(:payload AS jsonb), :created_at
                    )
                    """
                ),
                {
                    "id": stored.id,
                    "review_run_id": stored.review_run_id,
                    "claim_id": stored.claim_id,
                    "seq": stored.seq,
                    "from_agent": stored.from_agent,
                    "to_agent": stored.to_agent,
                    "kind": stored.kind,
                    "payload": json.dumps(stored.payload),
                    "created_at": datetime.fromisoformat(stored.created_at),
                },
            )
            await self.db.commit()
            return stored

    async def events(self, review_run_id: str) -> list[TraceEvent]:
        result = await self.db.execute(
            text(
                """
                SELECT id, review_run_id, claim_id, seq, from_agent, to_agent, kind, payload, created_at
                FROM agent_traces
                WHERE review_run_id = :run_id
                ORDER BY seq
                """
            ),
            {"run_id": review_run_id},
        )
        events = []
        for row in result:
            created = row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at)
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload or "{}")
            events.append(
                TraceEvent(
                    id=str(row.id),
                    review_run_id=str(row.review_run_id),
                    claim_id=str(row.claim_id),
                    seq=int(row.seq),
                    from_agent=row.from_agent,
                    to_agent=row.to_agent,
                    kind=row.kind,
                    payload=payload,
                    created_at=created,
                )
            )
        return events

    async def finish(self, claim_id: str, review_run_id: str, package: ReviewPackage) -> None:
        await self.db.execute(
            text(
                """
                UPDATE review_runs
                SET run_status = :status, package = CAST(:package AS jsonb), updated_at = :now
                WHERE id = :id
                """
            ),
            {
                "status": package.run_status,
                "package": package.model_dump_json(),
                "now": datetime.now(timezone.utc),
                "id": review_run_id,
            },
        )
        await self.db.commit()

    async def running_package(self, claim_id: str, review_run_id: str) -> ReviewPackage:
        result = await self.db.execute(
            text("SELECT package, run_status FROM review_runs WHERE id = :id"),
            {"id": review_run_id},
        )
        row = result.first()
        if row is not None and row.package:
            payload = row.package if isinstance(row.package, dict) else json.loads(row.package)
            return ReviewPackage.model_validate(payload)
        return ReviewPackage(
            review_run_id=review_run_id,
            claim_id=claim_id,
            run_status="running",
            recommended_action="manual_review",
            reasons=["REVIEW_RUNNING"],
            narrative="",
            trace=await self.events(review_run_id),
        )


def new_event(
    *,
    review_run_id: str,
    claim_id: str,
    from_agent: str,
    to_agent: str,
    kind: str,
    payload: dict[str, Any],
) -> TraceEvent:
    return TraceEvent(
        id=str(uuid4()),
        review_run_id=review_run_id,
        claim_id=claim_id,
        seq=0,
        from_agent=from_agent,  # type: ignore[arg-type]
        to_agent=to_agent,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        payload=payload,
        created_at=_now(),
    )
