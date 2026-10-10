import asyncio

from app.ai_agents.contracts.messages import SettlementRequest
from app.ai_agents.settlement.rules import assess_claim
from app.ai_agents.settlement.runner import run_settlement

CLEAN = {
    "claim_amount": 10000,
    "policy_status": "ACTIVE",
    "deductible": 0,
    "copay_percentage": 0,
    "remaining_balance": 480000,
}


class MemoryClaims:
    def __init__(self, claims: dict[str, dict | None]) -> None:
        self.claims = claims
        self.calls: list[str] = []

    async def load_claim(self, claim_id: str) -> dict | None:
        self.calls.append(claim_id)
        return self.claims.get(claim_id)


def _request(claim_id: str = "c1") -> SettlementRequest:
    return SettlementRequest(claim_id=claim_id, user_id="adjuster-1", role="INSURANCE_COMPANY")


def test_full_cover_is_approved():
    assessed = assess_claim(CLEAN)
    assert assessed["decision"] == "approve"
    assert assessed["payable_amount"] == 10000
    assert assessed["notes"] == []
    assert assessed["summary"] == "Approve 10000."


def test_deductible_and_copay_reduce_the_payable():
    assessed = assess_claim({**CLEAN, "deductible": 1000, "copay_percentage": 10})
    codes = [note["code"] for note in assessed["notes"]]
    assert assessed["decision"] == "approve"
    assert assessed["payable_amount"] == 8100
    assert codes == ["DEDUCTIBLE_APPLIED", "COPAY_APPLIED"]


def test_remaining_balance_caps_the_payment():
    assessed = assess_claim({**CLEAN, "remaining_balance": 4000})
    assert assessed["decision"] == "partial"
    assert assessed["payable_amount"] == 4000
    assert assessed["notes"][0]["code"] == "CAPPED_BY_BALANCE"


def test_inactive_policy_is_denied():
    assessed = assess_claim({**CLEAN, "policy_status": "LAPSED"})
    assert assessed["decision"] == "deny"
    assert assessed["payable_amount"] == 0
    assert assessed["notes"][0]["code"] == "POLICY_INACTIVE"


def test_amount_inside_deductible_is_denied():
    assessed = assess_claim({**CLEAN, "claim_amount": 500, "deductible": 1000})
    assert assessed["decision"] == "deny"
    assert assessed["payable_amount"] == 0
    assert assessed["notes"][0]["code"] == "WITHIN_DEDUCTIBLE"


def test_runner_approves_a_stored_claim():
    data = MemoryClaims({"c1": {**CLEAN, "deductible": 1000, "copay_percentage": 10}})
    result = asyncio.run(run_settlement(_request(), deps=data))
    assert result.status == "ok"
    assert result.agent == "settlement"
    assert result.decision == "approve"
    assert result.payable_amount == 8100
    assert data.calls == ["c1"]


def test_missing_claim_is_an_error():
    data = MemoryClaims({})
    result = asyncio.run(run_settlement(_request("missing"), deps=data))
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "BAD_INPUT"
    assert result.notes == []
