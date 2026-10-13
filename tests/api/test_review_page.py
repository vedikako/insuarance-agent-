from fastapi.testclient import TestClient

from app.ai_agents.contracts.messages import ReviewPackage, ReviewRequest
from app.api.policy_chat import Actor
from app.main import app

_STATE_KEYS = (
    "actor",
    "policy_data",
    "policy_qa_llm",
    "chat_sessions",
    "review_runner",
    "settlement_data",
    "claim_catalog",
)


def _clear_state() -> None:
    for key in _STATE_KEYS:
        if hasattr(app.state, key):
            delattr(app.state, key)


class MemoryClaims:
    def __init__(self, claims: dict[str, dict | None]) -> None:
        self.claims = claims
        self.calls: list[str] = []

    async def load_claim(self, claim_id: str) -> dict | None:
        self.calls.append(claim_id)
        return self.claims.get(claim_id)


def test_review_page_is_served():
    response = TestClient(app).get("/review")
    assert response.status_code == 200
    page = response.text
    assert "Review claim" in page
    assert "/api/reviews" in page
    assert "/api/settlements" in page
    assert "Payable amount" in page


def test_company_settlement_returns_the_payable_amount():
    data = MemoryClaims(
        {
            "c1": {
                "claim_amount": 10000,
                "policy_status": "ACTIVE",
                "deductible": 1000,
                "copay_percentage": 10,
                "remaining_balance": 480000,
            }
        }
    )
    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.settlement_data = data
    try:
        response = TestClient(app).post("/api/settlements", json={"claim_id": "c1"})
    finally:
        _clear_state()
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approve"
    assert body["payable_amount"] == 8100
    assert data.calls == ["c1"]


def test_customer_cannot_settle():
    data = MemoryClaims({"c1": {"claim_amount": 10000, "policy_status": "ACTIVE", "remaining_balance": 480000}})
    _clear_state()
    app.state.actor = Actor(user_id="u1", role="CUSTOMER")
    app.state.settlement_data = data
    try:
        response = TestClient(app).post("/api/settlements", json={"claim_id": "c1"})
    finally:
        _clear_state()
    assert response.status_code == 403
    assert data.calls == []


def test_company_can_pick_a_claim():
    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.claim_catalog = [
        {
            "id": "c1",
            "claim_number": "CLM-1",
            "claim_type": "HEALTH",
            "status": "SUBMITTED",
            "claim_amount": 10000,
            "policy_number": "POL-1",
            "user_id": "customer-1",
        }
    ]
    try:
        response = TestClient(app).get("/api/claims")
    finally:
        _clear_state()
    assert response.status_code == 200
    assert response.json()[0]["id"] == "c1"
    assert response.json()[0]["claim_number"] == "CLM-1"


def test_same_claim_can_be_reviewed_and_settled():
    seen: list[str] = []

    async def review(request: ReviewRequest, db=None):
        seen.append("review")
        package = ReviewPackage(
            review_run_id="run-1",
            claim_id=request.claim_id,
            run_status="ready",
            recommended_action="manual_review",
            narrative="A person still has to decide.",
        )
        return package

    data = MemoryClaims(
        {"c1": {"claim_amount": 10000, "policy_status": "ACTIVE", "remaining_balance": 4000}}
    )
    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.review_runner = review
    app.state.settlement_data = data
    client = TestClient(app)
    try:
        reviewed = client.post("/api/reviews", json={"claim_id": "c1"})
        settled = client.post("/api/settlements", json={"claim_id": "c1"})
    finally:
        _clear_state()
    assert reviewed.status_code == 200
    assert reviewed.json()["recommended_action"] == "manual_review"
    assert settled.status_code == 200
    assert settled.json()["decision"] == "partial"
    assert settled.json()["payable_amount"] == 4000
    assert seen == ["review"]
    assert data.calls == ["c1"]
