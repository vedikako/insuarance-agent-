import asyncio

from fastapi.testclient import TestClient

from app.ai_agents.contracts.messages import FraudReviewRequest
from app.ai_agents.fraud.rules import score_claim
from app.ai_agents.fraud.runner import run_fraud_review
from app.api.policy_chat import Actor
from app.main import app
from app.services.fraud_data import SqlFraudData, build_claim_snapshot

CLEAN = {
    "claim_amount": 10000,
    "approved_amount": None,
    "policy_status": "ACTIVE",
    "sum_insured": 500000,
    "remaining_balance": 480000,
    "other_claim_amounts": [2500],
}


class MemoryClaims:
    def __init__(self, claims: dict[str, dict | None]) -> None:
        self.claims = claims
        self.calls: list[str] = []

    async def load_claim(self, claim_id: str) -> dict | None:
        self.calls.append(claim_id)
        return self.claims.get(claim_id)


def _request(claim_id: str = "c1") -> FraudReviewRequest:
    return FraudReviewRequest(claim_id=claim_id, user_id="adjuster-1", role="INSURANCE_COMPANY")


def test_clean_claim_has_no_flags():
    scored = score_claim(CLEAN)
    assert scored["risk"] == "low"
    assert scored["flags"] == []
    assert scored["summary"] == "No fraud flags on this claim."


def test_amount_above_sum_insured_is_high():
    scored = score_claim({**CLEAN, "claim_amount": 600000})
    codes = [flag["code"] for flag in scored["flags"]]
    assert "CLAIM_OVER_SUM_INSURED" in codes
    assert "CLAIM_OVER_BALANCE" in codes
    assert scored["risk"] == "high"


def test_duplicate_amount_is_medium():
    scored = score_claim({**CLEAN, "other_claim_amounts": [10000]})
    assert scored["risk"] == "medium"
    assert scored["flags"][0]["code"] == "DUPLICATE_AMOUNT"


def test_missing_amount_and_approved_above_claimed():
    missing = score_claim({**CLEAN, "claim_amount": None})
    assert missing["risk"] == "medium"
    assert missing["flags"][0]["code"] == "MISSING_AMOUNT"
    approved = score_claim({**CLEAN, "approved_amount": 15000})
    assert approved["risk"] == "high"
    assert approved["flags"][0]["code"] == "APPROVED_ABOVE_CLAIMED"


def test_runner_returns_flags_for_a_stored_claim():
    data = MemoryClaims({"c1": {**CLEAN, "policy_status": "LAPSED"}})
    result = asyncio.run(run_fraud_review(_request(), deps=data))
    assert result.status == "ok"
    assert result.agent == "fraud"
    assert result.risk == "medium"
    assert result.flags[0].code == "POLICY_INACTIVE"
    assert data.calls == ["c1"]


def test_missing_claim_is_an_error():
    data = MemoryClaims({})
    result = asyncio.run(run_fraud_review(_request("missing"), deps=data))
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "BAD_INPUT"
    assert result.flags == []


def test_invalid_claim_id_is_not_queried():
    class Session:
        called = False

        async def execute(self, *args, **kwargs):
            self.called = True
            raise AssertionError("an invalid id must not be sent to the database")

    session = Session()
    result = asyncio.run(SqlFraudData(session).load_claim("missing-claim"))  # type: ignore[arg-type]
    assert result is None
    assert session.called is False


def test_snapshot_uses_ledger_remaining_and_other_amounts():
    snapshot = build_claim_snapshot(
        claim_amount=10000,
        approved_amount=None,
        policy_status="ACTIVE",
        sum_insured=500000,
        utilized=20000,
        other_claim_amounts=[2500, None],
    )
    assert snapshot["remaining_balance"] == 480000
    assert snapshot["sum_insured"] == 500000
    assert snapshot["other_claim_amounts"] == [2500]


def _client(actor: Actor, claims: MemoryClaims) -> TestClient:
    app.state.actor = actor
    app.state.fraud_data = claims
    return TestClient(app)


def test_customer_cannot_review_a_claim():
    data = MemoryClaims({"c1": dict(CLEAN)})
    client = _client(Actor(user_id="u1", role="CUSTOMER"), data)
    response = client.post("/api/fraud/reviews", json={"claim_id": "c1"})
    assert response.status_code == 403
    assert data.calls == []


def test_adjuster_receives_a_review():
    data = MemoryClaims({"c1": dict(CLEAN)})
    client = _client(Actor(user_id="adj-1", role="INSURANCE_COMPANY"), data)
    response = client.post("/api/fraud/reviews", json={"claim_id": "c1"})
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "fraud"
    assert body["risk"] == "low"
    assert body["flags"] == []
    assert data.calls == ["c1"]


def test_unknown_claim_is_not_found():
    data = MemoryClaims({})
    client = _client(Actor(user_id="adj-1", role="AUDITOR"), data)
    response = client.post("/api/fraud/reviews", json={"claim_id": "missing"})
    assert response.status_code == 404


def test_review_requires_sign_in():
    app.state.actor = None
    app.state.fraud_data = MemoryClaims({"c1": dict(CLEAN)})
    response = TestClient(app).post("/api/fraud/reviews", json={"claim_id": "c1"})
    assert response.status_code == 401


def test_lookup_failure_returns_503():
    class BrokenClaims:
        async def load_claim(self, claim_id: str) -> dict | None:
            raise RuntimeError("database unavailable")

    app.state.actor = Actor(user_id="adj-1", role="INSURANCE_COMPANY")
    app.state.fraud_data = BrokenClaims()
    response = TestClient(app).post("/api/fraud/reviews", json={"claim_id": "c1"})
    assert response.status_code == 503
    assert response.json()["detail"] == "The claim lookup failed."
