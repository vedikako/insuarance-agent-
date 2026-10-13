from fastapi.testclient import TestClient

from app.ai_agents.contracts.messages import ReviewPackage, ReviewRequest
from app.ai_agents.orchestrator.runner import document_refs_from_rows, id_last4
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


def _package(claim_id: str, *, narrative: str = "A person still has to decide.", status: str = "ready") -> ReviewPackage:
    return ReviewPackage(
        review_run_id="run-1",
        claim_id=claim_id,
        run_status=status,  # type: ignore[arg-type]
        recommended_action="manual_review",
        narrative=narrative,
    )


def test_document_rows_keep_known_files_only():
    refs = document_refs_from_rows(
        [
            {
                "id": "bill-1",
                "document_type_code": "hospital_bill",
                "storage_path": "files/bill.pdf",
                "original_filename": "bill.pdf",
                "content_type": "application/pdf",
            },
            {
                "id": "note-1",
                "document_type_code": "CLAIM_FORM",
                "storage_path": "files/form.pdf",
                "original_filename": "form.pdf",
                "content_type": "application/pdf",
            },
            {
                "id": "id-1",
                "document_type_code": "AADHAAR",
                "storage_path": "files/id.jpg",
                "original_filename": "",
                "content_type": "image/jpg",
            },
        ]
    )
    assert [item["type_code"] for item in refs] == ["HOSPITAL_BILL", "AADHAAR"]
    assert refs[1]["mime"] == "image/jpeg"
    assert refs[1]["filename"] == "aadhaar.bin"
    assert id_last4("XXXX-1234") == "1234"
    assert id_last4("12") is None


def test_company_can_start_a_review():
    seen: list[ReviewRequest] = []

    async def runner(request: ReviewRequest, db=None):
        seen.append(request)
        assert db is None
        return _package(request.claim_id)

    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.review_runner = runner
    try:
        response = TestClient(app).post("/api/reviews", json={"claim_id": "claim-1"})
    finally:
        _clear_state()
    assert response.status_code == 200
    body = response.json()
    assert body["claim_id"] == "claim-1"
    assert body["run_status"] == "ready"
    assert body["recommended_action"] == "manual_review"
    assert seen[0].user_id == "adjuster-1"
    assert seen[0].role == "INSURANCE_COMPANY"


def test_auditor_and_customer_cannot_start_a_review():
    calls = 0

    async def runner(request: ReviewRequest, db=None):
        nonlocal calls
        calls += 1
        return _package(request.claim_id)

    for role in ("AUDITOR", "CUSTOMER"):
        _clear_state()
        app.state.actor = Actor(user_id="u1", role=role)  # type: ignore[arg-type]
        app.state.review_runner = runner
        try:
            response = TestClient(app).post("/api/reviews", json={"claim_id": "claim-1"})
        finally:
            _clear_state()
        assert response.status_code == 403
    assert calls == 0


def test_missing_claim_is_not_found():
    async def runner(request: ReviewRequest, db=None):
        return _package(request.claim_id, narrative="Claim not found.", status="failed")

    _clear_state()
    app.state.actor = Actor(user_id="adjuster-1", role="INSURANCE_COMPANY")
    app.state.review_runner = runner
    try:
        response = TestClient(app).post("/api/reviews", json={"claim_id": "missing"})
    finally:
        _clear_state()
    assert response.status_code == 404


def test_anonymous_request_is_rejected():
    async def runner(request: ReviewRequest, db=None):
        raise AssertionError("review should not start")

    _clear_state()
    app.state.review_runner = runner
    try:
        response = TestClient(app).post("/api/reviews", json={"claim_id": "claim-1"})
    finally:
        _clear_state()
    assert response.status_code == 401
