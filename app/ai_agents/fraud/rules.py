from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _flag(code: str, severity: str, detail: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


def score_claim(claim: dict[str, Any]) -> dict[str, Any]:
    flags: list[dict[str, str]] = []
    amount = _money(claim.get("claim_amount"))
    approved = _money(claim.get("approved_amount"))
    sum_insured = _money(claim.get("sum_insured"))
    remaining = _money(claim.get("remaining_balance"))

    if amount is None:
        flags.append(_flag("MISSING_AMOUNT", "medium", "This claim has no amount stored."))
    over_sum = amount is not None and sum_insured is not None and amount > sum_insured
    if over_sum:
        flags.append(
            _flag(
                "CLAIM_OVER_SUM_INSURED",
                "high",
                "The claim amount is above the policy sum insured.",
            )
        )
    if (
        not over_sum
        and amount is not None
        and remaining is not None
        and amount > remaining
    ):
        flags.append(
            _flag(
                "CLAIM_OVER_BALANCE",
                "medium",
                "The claim amount is above the remaining balance.",
            )
        )
    if amount is not None and amount > 0:
        others = [_money(item) for item in claim.get("other_claim_amounts") or []]
        if any(other == amount for other in others):
            flags.append(
                _flag(
                    "DUPLICATE_AMOUNT",
                    "medium",
                    "Another claim on this policy has the same amount.",
                )
            )
    policy_status = str(claim.get("policy_status") or "").upper()
    if policy_status and policy_status != "ACTIVE":
        flags.append(_flag("POLICY_INACTIVE", "medium", "The policy is not active."))
    if amount is not None and approved is not None and approved > amount:
        flags.append(
            _flag(
                "APPROVED_ABOVE_CLAIMED",
                "high",
                "The approved amount is above the claimed amount.",
            )
        )

    if any(flag["severity"] == "high" for flag in flags):
        risk = "high"
    elif flags:
        risk = "medium"
    else:
        risk = "low"
    if flags:
        summary = "Manual review. " + " ".join(flag["detail"] for flag in flags)
    else:
        summary = "No fraud flags on this claim."
    return {"risk": risk, "flags": flags, "summary": summary}
