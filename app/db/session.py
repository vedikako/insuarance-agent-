from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        if not settings.DATABASE_URL:
            raise HTTPException(status_code=503, detail="Database is not configured")
        engine = create_async_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return _sessionmaker


async def get_db(request: Request) -> AsyncGenerator[AsyncSession | None, None]:
async def get_db(request: Request) -> AsyncGenerator[AsyncSession | None, None]:
    """Yield a session. Tests that inject data ports skip the database."""
    state = request.app.state
    if (
        getattr(state, "policy_data", None) is not None
        or getattr(state, "fraud_data", None) is not None
        or getattr(state, "review_runner", None) is not None
    ):
        yield None
        return
        
    factory = get_sessionmaker()
    async with factory() as session:
        yield session
