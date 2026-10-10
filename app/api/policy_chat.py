from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_agents.contracts.messages import PolicyQARequest
from app.ai_agents.policy_qa.llm import GeminiJsonLLM, JsonLLM
from app.ai_agents.policy_qa.runner import run_policy_qa
from app.api.access import can_read_policy
from app.config import settings
from app.core.security import decode_token
from app.db.session import get_db
from app.services.policy_data import PolicyDataPort, SqlPolicyData

router = APIRouter(prefix="/policy-chat", tags=["policy-chat"])


class Actor(BaseModel):
    user_id: str
    role: Literal["CUSTOMER", "INSURANCE_COMPANY", "AUDITOR"]


class CreateSessionIn(BaseModel):
    policy_id: str


class SessionOut(BaseModel):
    session_id: str
    policy_id: str


class MessageIn(BaseModel):
    message: str = Field(min_length=1)


class SessionRecord(BaseModel):
    session_id: str
    user_id: str
    policy_id: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def create(self, user_id: str, policy_id: str) -> SessionRecord:
        record = SessionRecord(session_id=str(uuid.uuid4()), user_id=user_id, policy_id=policy_id)
        self._sessions[record.session_id] = record
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)


def _actor_role(role: str) -> str:
    if role in {"INSURER_ADMIN", "CLAIM_ADJUSTER", "INSURANCE_COMPANY"}:
        return "INSURANCE_COMPANY"
    if role == "AUDITOR":
        return "AUDITOR"
    if role == "CUSTOMER":
        return "CUSTOMER"
    raise HTTPException(status_code=403, detail="This role cannot use policy chat")


async def get_actor(
    request: Request,
    db: AsyncSession | None = Depends(get_db),
) -> Actor:
    override = getattr(request.app.state, "actor", None)
    if override is not None:
        return override
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalid or expired")
    if payload.get("type") != "access" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    result = await db.execute(
        text("SELECT id, role, is_active FROM users WHERE id = :id"),
        {"id": payload["sub"]},
    )
    user = result.first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return Actor(user_id=str(user.id), role=_actor_role(user.role))


async def get_policy_data(
    request: Request,
    db: AsyncSession | None = Depends(get_db),
) -> PolicyDataPort:
    port = getattr(request.app.state, "policy_data", None)
    if port is not None:
        return port
    if db is None:
        raise HTTPException(status_code=503, detail="Policy data is not configured")
    return SqlPolicyData(db)


def get_store(request: Request) -> MemorySessionStore:
    store = getattr(request.app.state, "chat_sessions", None)
    if store is None:
        store = MemorySessionStore()
        request.app.state.chat_sessions = store
    return store


def get_llm(request: Request) -> JsonLLM:
    llm = getattr(request.app.state, "policy_qa_llm", None)
    if llm is not None:
        return llm
    api_key = settings.GCP_API_KEY or getattr(request.app.state, "gcp_api_key", "") or ""
    if not api_key:
        raise HTTPException(status_code=503, detail="Policy assistant model is not configured")
    return GeminiJsonLLM(api_key)


def _refuse_if_forbidden(actor: Actor, owner_id: str | None) -> None:
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    if not can_read_policy(actor.role, actor.user_id, owner_id):
        raise HTTPException(status_code=403, detail="You cannot access this policy")


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: CreateSessionIn,
    actor: Actor = Depends(get_actor),
    data: PolicyDataPort = Depends(get_policy_data),
    store: MemorySessionStore = Depends(get_store),
) -> SessionOut:
    owner_id = await data.get_owner_id(body.policy_id)
    _refuse_if_forbidden(actor, owner_id)
    record = store.create(actor.user_id, body.policy_id)
    return SessionOut(session_id=record.session_id, policy_id=record.policy_id)


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    body: MessageIn,
    actor: Actor = Depends(get_actor),
    data: PolicyDataPort = Depends(get_policy_data),
    store: MemorySessionStore = Depends(get_store),
    llm: JsonLLM = Depends(get_llm),
):
    record = store.get(session_id)
    if record is None or record.user_id != actor.user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    owner_id = await data.get_owner_id(record.policy_id)
    _refuse_if_forbidden(actor, owner_id)
    result = await run_policy_qa(
        PolicyQARequest(
            session_id=record.session_id,
            user_id=actor.user_id,
            role=actor.role,
            policy_id=record.policy_id,
            message=body.message,
            history=record.history[-10:],
        ),
        deps=data,
        llm=llm,
    )
    record.history.append({"role": "user", "content": body.message})
    record.history.append({"role": "assistant", "content": result.reply})
    return result
