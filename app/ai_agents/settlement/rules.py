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


def _note(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _amount_text(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral():
        return str(quantized.to_integral())
    return f"{quantized:.2f}"


def _payable_float(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _result(
    decision: str,
    payable: Decimal | None,
    notes: list[dict[str, str]],
    summary: str,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "payable_amount": None if payable is None else _payable_float(payable),
        "notes": notes,
        "summary": summary,
    }


def assess_claim(claim: dict[str, Any]) -> dict[str, Any]:
    notes: list[dict[str, str]] = []
    amount = _money(claim.get("claim_amount"))
    deductible = _money(claim.get("deductible"))
    copay = _money(claim.get("copay_percentage"))
    remaining = _money(claim.get("remaining_balance"))

    if amount is None:
        notes.append(_note("MISSING_AMOUNT", "This claim has no amount stored."))
        return _result("review", None, notes, "Manual review. This claim has no amount stored.")
    if deductible is not None and deductible < 0:
        notes.append(_note("INVALID_DEDUCTIBLE", "The deductible is negative."))
        return _result("review", None, notes, "Manual review. The deductible is negative.")
    if copay is not None and (copay < 0 or copay > 100):
        notes.append(_note("INVALID_COPAY", "The copay percentage is outside 0 to 100."))
        return _result("review", None, notes, "Manual review. The copay percentage is outside 0 to 100.")

    policy_status = str(claim.get("policy_status") or "").upper()
    if policy_status and policy_status != "ACTIVE":
        notes.append(_note("POLICY_INACTIVE", "The policy is not active."))
        return _result("deny", Decimal("0"), notes, "Deny. The policy is not active.")
    if amount <= 0:
        notes.append(_note("NON_POSITIVE_AMOUNT", "The claim amount is not positive."))
        return _result("deny", Decimal("0"), notes, "Deny. The claim amount is not positive.")

    deductible_value = deductible or Decimal("0")
    copay_value = copay or Decimal("0")
    covered = amount - deductible_value
    if covered <= 0:
        notes.append(_note("WITHIN_DEDUCTIBLE", "The claim amount is within the deductible."))
        return _result("deny", Decimal("0"), notes, "Deny. The claim amount is within the deductible.")

    if deductible_value > 0:
        notes.append(
            _note("DEDUCTIBLE_APPLIED", f"A deductible of {_amount_text(deductible_value)} was applied.")
        )
    insurer_share = (Decimal("100") - copay_value) / Decimal("100")
    payable = (covered * insurer_share).quantize(Decimal("0.01"))
    if copay_value > 0:
        notes.append(_note("COPAY_APPLIED", f"A copay of {_amount_text(copay_value)} percent was applied."))

    decision = "approve"
    if remaining is not None and payable > remaining:
        notes.append(_note("CAPPED_BY_BALANCE", "The payable amount is capped at the remaining balance."))
        payable = remaining.quantize(Decimal("0.01"))
        decision = "partial" if payable > 0 else "deny"

    if decision == "deny":
        summary = "Deny. The remaining balance is zero."
    elif decision == "partial":
        summary = f"Partial payment of {_amount_text(payable)}. " + " ".join(note["detail"] for note in notes)
    else:
        summary = f"Approve {_amount_text(payable)}."
        if notes:
            summary = f"{summary} " + " ".join(note["detail"] for note in notes)
    return _result(decision, payable, notes, summary)
