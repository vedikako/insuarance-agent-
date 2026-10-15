from fastapi.testclient import TestClient

from app.api.policy_chat import Actor
from app.main import app

_STATE_KEYS = (
    "actor",
    "policy_data",
    "fraud_data",
    "policy_qa_llm",
    "chat_sessions",
    "review_runner",
    "settlement_data",
    "claim_catalog",
    "claim_records",
    "policy_catalog",
)


def _clear_state() -> None:
    for key in _STATE_KEYS:
        if hasattr(app.state, key):
            delattr(app.state, key)


def _claim() -> dict:
    return {
        "c1": {
            "id": "c1",
            "user_id": "cust",
            "claim_number": "CLM-1",
            "claim_type": "HEALTH",
            "status": "SUBMITTED",
            "claim_amount": 10000,
            "approved_amount": None,
            "policy_number": "POL-1",
            "description": "Fever",
        }
    }


def test_review_page_offers_accept_and_reject():
    page = TestClient(app).get("/review").text
    assert "Accept" in page
    assert "Reject" in page
    assert "/decision" in page


def test_accept_saves_approved_on_the_claim():
    records = _claim()
    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.claim_records = records
    try:
        response = TestClient(app).post(
            "/api/claims/c1/decision",
            json={"choice": "accept", "payable_amount": 8100},
        )
    finally:
        _clear_state()
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert response.json()["approved_amount"] == 8100
    assert records["c1"]["status"] == "APPROVED"


def test_reject_saves_rejected_on_the_claim():
    records = _claim()
    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.claim_records = records
    try:
        response = TestClient(app).post("/api/claims/c1/decision", json={"choice": "reject"})
    finally:
        _clear_state()
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert response.json()["approved_amount"] is None
    assert records["c1"]["adjuster_notes"] == "Rejected by the insurer."


def test_customer_cannot_decide():
    records = _claim()
    _clear_state()
    app.state.actor = Actor(user_id="cust", role="CUSTOMER")
    app.state.claim_records = records
    try:
        response = TestClient(app).post("/api/claims/c1/decision", json={"choice": "accept"})
    finally:
        _clear_state()
    assert response.status_code == 403
    assert records["c1"]["status"] == "SUBMITTED"


def test_customer_files_a_claim_and_sees_it():
    _clear_state()
    app.state.actor = Actor(user_id="u1", role="CUSTOMER")
    app.state.claim_records = {}
    app.state.policy_catalog = [
        {
            "id": "p1",
            "user_id": "u1",
            "policy_number": "POL-1",
            "policy_type": "HEALTH",
            "status": "ACTIVE",
            "sum_insured": 500000,
        }
    ]
    client = TestClient(app)
    try:
        filed = client.post(
            "/api/claims",
            data={
                "policy_id": "p1",
                "claim_amount": "2500",
                "description": "Hospital stay",
                "incident_date": "2026-06-01",
                "document_types": "HOSPITAL_BILL",
            },
            files={"files": ("bill.pdf", b"%PDF-1.4", "application/pdf")},
        )
        listed = client.get("/api/claims")
    finally:
        _clear_state()
    assert filed.status_code == 201
    body = filed.json()
    assert body["status"] == "SUBMITTED"
    assert body["documents"][0]["type_code"] == "HOSPITAL_BILL"
    assert listed.status_code == 200
    assert listed.json()[0]["claim_number"] == body["claim_number"]
    assert listed.json()[0]["status"] == "SUBMITTED"


def test_customer_cannot_file_on_someone_elses_policy():
    _clear_state()
    app.state.actor = Actor(user_id="u1", role="CUSTOMER")
    app.state.claim_records = {}
    app.state.policy_catalog = [
        {
            "id": "p2",
            "user_id": "u2",
            "policy_number": "POL-2",
            "policy_type": "HEALTH",
            "status": "ACTIVE",
            "sum_insured": 500000,
        }
    ]
    try:
        response = TestClient(app).post(
            "/api/claims",
            data={
                "policy_id": "p2",
                "claim_amount": "100",
                "description": "",
                "incident_date": "",
                "document_types": "PAN",
            },
            files={"files": ("pan.png", b"png", "image/png")},
        )
    finally:
        _clear_state()
    assert response.status_code == 403


def test_file_and_status_pages_are_served():
    client = TestClient(app)
    assert "Submit claim" in client.get("/file").text
    assert "Claim status" in client.get("/status").text
