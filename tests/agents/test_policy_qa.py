import asyncio
import json

from fastapi.testclient import TestClient

from app.ai_agents.contracts.messages import PolicyQARequest
from app.ai_agents.policy_qa.runner import LEDGER_ONLY, enforce_amount_rule, run_policy_qa
from app.api.policy_chat import Actor, MemorySessionStore
from app.main import app
from app.services.policy_retrieval import rank_chunks

CATARACT = {
    "chunk_id": "c1",
    "section_path": "4.2 Day care",
    "quote": "Cataract surgery is covered as a day care procedure.",
}
BALANCE = {
    "sum_insured": 500000,
    "utilized": 20000,
    "remaining_balance": 480000,
    "currency": "INR",
}


class ScriptLLM:
    async def complete(self, system: str, user: str) -> str:
        if system.startswith("You are the supervisor"):
            latest = user.split("Latest message:")[-1].strip().lower()
            if latest.startswith("hello") or latest.startswith("thanks"):
                return json.dumps(
                    {
                        "route": "direct",
                        "reason": "greeting",
                        "tools_needed": [],
                        "direct_reply": "Hello. Ask about cover or the amount left on this policy.",
                    }
                )
            if "how much" in latest or "still claim" in latest:
                return json.dumps(
                    {
                        "route": "data",
                        "reason": "asks for remaining cover",
                        "tools_needed": ["get_balance"],
                        "direct_reply": "",
                    }
                )
            return json.dumps(
                {
                    "route": "data",
                    "reason": "asks about wording",
                    "tools_needed": ["retrieve_clauses"],
                    "direct_reply": "",
                }
            )
        if system.startswith("Rewrite"):
            return json.dumps({"query": "cataract day care procedure"})
        payload = json.loads(user)
        context = payload["data_context"]
        if "get_balance" in context:
            left = int(context["get_balance"]["remaining_balance"])
            return json.dumps({"reply": f"You have {left} INR left to claim.", "cited_chunk_ids": []})
        chunks = context.get("retrieve_clauses") or []
        if chunks:
            quote = chunks[0]["quote"]
            return json.dumps(
                {"reply": f"From this policy: {quote}", "cited_chunk_ids": [chunks[0]["chunk_id"]]}
            )
        return json.dumps(
            {
                "reply": "I could not find that wording in this policy's documents.",
                "cited_chunk_ids": [],
            }
        )


class MemoryPolicies:
    def __init__(self) -> None:
        self.retrieve_calls: list[str] = []
        self.balance_calls = 0
        self.owner = {"p1": "u1", "p2": "u2"}

    async def get_owner_id(self, policy_id: str) -> str | None:
        return self.owner.get(policy_id)

    async def get_balance(self, policy_id: str) -> dict:
        self.balance_calls += 1
        return dict(BALANCE)

    async def list_utilization_entries(self, policy_id: str) -> list:
        return []

    async def get_policy_facts(self, policy_id: str) -> dict:
        return {
            "policy_number": "POL-1",
            "status": "ACTIVE",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "sum_insured": 500000,
            "premium_amount": 12000,
            "deductible": 0,
            "copay_percentage": 0,
            "insured_name": "Asha Rao",
        }

    async def retrieve_clauses(self, policy_id: str, query: str, k: int = 4) -> list:
        self.retrieve_calls.append(query)
        if "day care" in query.lower():
            return [dict(CATARACT)]
        if "missing wording" in query.lower():
            return []
        return [dict(CATARACT)]

    async def list_claims_for_policy(self, policy_id: str) -> list:
        return [{"claim_number": "CLM-1", "status": "SUBMITTED", "claim_amount": 1000, "approved_amount": None}]


def _request(message: str, policy_id: str = "p1", user_id: str = "u1") -> PolicyQARequest:
    return PolicyQARequest(
        session_id="s1",
        user_id=user_id,
        role="CUSTOMER",
        policy_id=policy_id,
        message=message,
        history=[],
    )


def test_rank_chunks_prefers_matching_wording():
    ranked = rank_chunks(
        [
            {"chunk_id": "a", "section_path": "1", "content": "Room rent is capped per day."},
            {"chunk_id": "b", "section_path": "2", "content": "Cataract surgery is covered as day care."},
        ],
        "cataract surgery",
    )
    assert ranked[0]["chunk_id"] == "b"
    assert "Cataract" in ranked[0]["quote"]


def test_hello_skips_data_node():
    data = MemoryPolicies()
    result = asyncio.run(run_policy_qa(_request("Hello"), deps=data, llm=ScriptLLM()))
    assert result.status == "ok"
    assert result.route == "direct"
    assert result.tool_calls == []
    assert result.citations == []
    assert result.balance is None
    assert data.balance_calls == 0
    assert data.retrieve_calls == []


def test_balance_uses_ledger_figure():
    data = MemoryPolicies()
    result = asyncio.run(run_policy_qa(_request("How much can I still claim?"), deps=data, llm=ScriptLLM()))
    assert result.route == "data"
    assert [call.tool_name for call in result.tool_calls] == ["get_balance"]
    assert "480000" in result.reply
    assert "999999" not in result.reply
    assert result.balance["remaining_balance"] == 480000


def test_cataract_quotes_returned_chunk():
    data = MemoryPolicies()
    result = asyncio.run(run_policy_qa(_request("Is cataract covered?"), deps=data, llm=ScriptLLM()))
    assert result.route == "data"
    assert result.tool_calls[0].tool_name == "retrieve_clauses"
    assert result.citations[0].chunk_id == "c1"
    assert "Cataract surgery is covered as a day care procedure." in result.reply


def test_empty_clause_search_rewrites_once():
    class EmptyThenHit(MemoryPolicies):
        async def retrieve_clauses(self, policy_id: str, query: str, k: int = 4) -> list:
            self.retrieve_calls.append(query)
            if "day care" in query.lower():
                return [dict(CATARACT)]
            return []

    data = EmptyThenHit()
    result = asyncio.run(run_policy_qa(_request("Is cataract covered?"), deps=data, llm=ScriptLLM()))
    assert len(data.retrieve_calls) == 2
    assert "day care" in data.retrieve_calls[1].lower()
    assert result.citations[0].chunk_id == "c1"


def test_invented_amount_is_replaced():
    reply = enforce_amount_rule(
        "You have 999999 INR left.",
        {"get_balance": BALANCE},
        [],
    )
    assert reply == LEDGER_ONLY


def test_balance_answers_when_the_model_is_down():
    class DownLLM:
        async def complete(self, system: str, user: str) -> str:
            raise RuntimeError("503")

    result = asyncio.run(
        run_policy_qa(_request("How much can I still claim?"), deps=MemoryPolicies(), llm=DownLLM())
    )
    assert result.status == "ok"
    assert result.route == "data"
    assert [call.tool_name for call in result.tool_calls] == ["get_balance"]
    assert "480000" in result.reply
    assert "20000" in result.reply


def test_supervisor_failure_still_reads_wording():
    class SupervisorDown(ScriptLLM):
        async def complete(self, system: str, user: str) -> str:
            if system.startswith("You are the supervisor"):
                raise RuntimeError("503")
            return await ScriptLLM.complete(self, system, user)

    result = asyncio.run(run_policy_qa(_request("Is cataract covered?"), deps=MemoryPolicies(), llm=SupervisorDown()))
    assert result.status == "ok"
    assert result.tool_calls[0].tool_name == "retrieve_clauses"
    assert "Cataract surgery is covered as a day care procedure." in result.reply


def test_customer_cannot_open_another_users_policy():
    data = MemoryPolicies()
    app.state.actor = Actor(user_id="u1", role="CUSTOMER")
    app.state.policy_data = data
    app.state.policy_qa_llm = ScriptLLM()
    app.state.chat_sessions = MemorySessionStore()
    client = TestClient(app)
    response = client.post("/api/policy-chat/sessions", json={"policy_id": "p2"})
    assert response.status_code == 403
    assert data.balance_calls == 0
    assert data.retrieve_calls == []


def test_owner_can_open_a_session_and_ask():
    data = MemoryPolicies()
    app.state.actor = Actor(user_id="u1", role="CUSTOMER")
    app.state.policy_data = data
    app.state.policy_qa_llm = ScriptLLM()
    app.state.chat_sessions = MemorySessionStore()
    client = TestClient(app)
    created = client.post("/api/policy-chat/sessions", json={"policy_id": "p1"})
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    reply = client.post(f"/api/policy-chat/sessions/{session_id}/messages", json={"message": "Hello"})
    assert reply.status_code == 200
    body = reply.json()
    assert body["route"] == "direct"
    assert body["tool_calls"] == []
