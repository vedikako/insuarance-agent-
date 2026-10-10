import asyncio

from app.ai_agents.contracts.messages import FraudReviewRequest
from app.ai_agents.fraud.rules import score_claim
from app.ai_agents.fraud.runner import run_fraud_review

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
