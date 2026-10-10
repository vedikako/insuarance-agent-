"""Legal next action for a claim review. First matching rule wins."""

from __future__ import annotations

from typing import Any

Action = str

ACTIONS = (
    "CALL_DOCUMENT",
    "CALL_ELIGIBILITY_AND_FRAUD",
    "REVISE_DOCUMENT",
    "REVISE_ELIGIBILITY",
    "REVISE_FRAUD",
    "ESCALATE",
    "FINISH",
)

_CRITICAL_FLAGS = {
    "COPY_MOVE_DETECTED",
    "PHASH_DUPLICATE",
    "ARITHMETIC_MISMATCH",
    "CLAIM_BEFORE_POLICY",
    "CONTENT_CRITICAL",
    "ID_MISMATCH",
}
_WATCHED = ("bill_total", "holder_name", "admission_date")
_POSITIVE = {"eligible", "partially_eligible"}
_HIGH = {"HIGH", "VERY_HIGH"}
_ACCEPTABLE_RISK = {"MINIMAL", "LOW", "MEDIUM"}


def _calls(state: dict[str, Any], name: str) -> int:
    return int(state.get(name) or 0)


def _failed(result: dict[str, Any] | None) -> bool:
    return not result or result.get("status") == "error"


def _failure_reason(result: dict[str, Any] | None) -> str:
    error = (result or {}).get("error") or {}
    if error.get("code") == "TIMEOUT":
        return "TIMEOUT"
    return "TOOL_FAILED"


def _decide(state: dict[str, Any]) -> tuple[str, str, str]:
    document = state.get("document")
    eligibility = state.get("eligibility")
    fraud = state.get("fraud")
    document_calls = _calls(state, "document_calls")
    eligibility_calls = _calls(state, "eligibility_calls")
    fraud_calls = _calls(state, "fraud_calls")

    if document_calls == 0:
        return "CALL_DOCUMENT", "", ""
    if _failed(document):
        return "ESCALATE", _failure_reason(document), ""

    package_status = str((document or {}).get("package_status") or "")
    if package_status == "kyc_failed":
        return "ESCALATE", "KYC_FAILED", ""
    if package_status == "needs_reupload":
        return "ESCALATE", "NEEDS_REUPLOAD", ""
    if package_status not in {"accepted", "partial"}:
        return "ESCALATE", "NEEDS_REUPLOAD", ""

    if state.get("needs_followup"):
        if eligibility_calls >= 2:
            return "ESCALATE", "REVISION_CAP", ""
        if state.get("watched_fields_changed") and fraud_calls < 2:
            return "CALL_ELIGIBILITY_AND_FRAUD", "", "fields"
        return "REVISE_ELIGIBILITY", "", "fields"

    if eligibility_calls == 0 and fraud_calls == 0:
        return "CALL_ELIGIBILITY_AND_FRAUD", "", ""
    if _failed(eligibility) or _failed(fraud):
        failed = eligibility if _failed(eligibility) else fraud
        return "ESCALATE", _failure_reason(failed), ""

    critical = bool((fraud or {}).get("critical"))
    disputes = (fraud or {}).get("disputes") or []
    groundedness = int((eligibility or {}).get("groundedness_score") or 0)
    missing = (document or {}).get("missing_fields") or []
    decision = (eligibility or {}).get("decision")
    risk = (fraud or {}).get("risk_level")
    positive = decision in _POSITIVE
    high = risk in _HIGH

    if critical and disputes and eligibility_calls == 1:
        return "REVISE_ELIGIBILITY", "", "disputes"
    if critical and disputes and eligibility_calls >= 2:
        return "ESCALATE", "CONFLICT_UNRESOLVED", ""
    if groundedness < 4 and missing and document_calls == 1:
        return "REVISE_DOCUMENT", "", "focus"
    if groundedness < 4 and missing and document_calls >= 2:
        return "ESCALATE", "REVISION_CAP", ""
    if groundedness < 4 and not missing and eligibility_calls == 1:
        return "REVISE_ELIGIBILITY", "", "unsupported"
    if groundedness < 4 and not missing and eligibility_calls >= 2:
        return "FINISH", "LOW_GROUNDEDNESS", ""
    if positive and high and fraud_calls == 1:
        return "REVISE_FRAUD", "", "fraud_flag"
    if positive and high and fraud_calls >= 2:
        return "ESCALATE", "CONFLICT_UNRESOLVED", ""
    return "FINISH", "", ""


def next_action(state: dict[str, Any]) -> Action:
    return _decide(state)[0]


def decision_reason(state: dict[str, Any]) -> str:
    return _decide(state)[1]


def revision_kind(state: dict[str, Any]) -> str:
    return _decide(state)[2]


def _would_exceed_cap(proposal: str, state: dict[str, Any]) -> bool:
    document_calls = _calls(state, "document_calls")
    eligibility_calls = _calls(state, "eligibility_calls")
    fraud_calls = _calls(state, "fraud_calls")
    if proposal in {"CALL_DOCUMENT", "REVISE_DOCUMENT"}:
        return document_calls >= 2
    if proposal == "REVISE_ELIGIBILITY":
        return eligibility_calls >= 2
    if proposal == "REVISE_FRAUD":
        return fraud_calls >= 2
    if proposal == "CALL_ELIGIBILITY_AND_FRAUD":
        return eligibility_calls >= 2 or fraud_calls >= 2
    return False


def resolve_proposal(state: dict[str, Any], proposal: str | None) -> tuple[str, str | None, bool]:
    """Return the action to run, an escalate-reason override, and whether the proposal was rejected."""
    legal = next_action(state)
    if proposal is None or proposal == legal:
        return legal, None, False
    if proposal not in ACTIONS or _would_exceed_cap(proposal, state):
        if proposal in ACTIONS and _would_exceed_cap(proposal, state):
            return "ESCALATE", "REVISION_CAP", True
        return legal, None, True
    return legal, None, True


def top_flag(fraud: dict[str, Any]) -> str:
    flags = [str(flag) for flag in fraud.get("flags") or []]
    for flag in flags:
        if flag in _CRITICAL_FLAGS:
            return flag
    if flags:
        return flags[0]
    return "ARITHMETIC_MISMATCH"


def watched_snapshot(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: fields.get(key) for key in _WATCHED}


def watched_changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [key for key in _WATCHED if before.get(key) != after.get(key)]


def _disputes_cleared(eligibility: dict[str, Any], fraud: dict[str, Any]) -> bool:
    disputes = fraud.get("disputes") or []
    if not disputes:
        return True
    answered = {
        str(item.get("code")): item.get("stance")
        for item in eligibility.get("answered_disputes") or []
    }
    return all(answered.get(str(item.get("code"))) in {"sustain", "revise"} for item in disputes)


def recommended_action(state: dict[str, Any], *, escalated: bool) -> str:
    if escalated:
        return "manual_review"
    document = state.get("document") or {}
    eligibility = state.get("eligibility") or {}
    fraud = state.get("fraud") or {}
    if _failed(document) or _failed(eligibility) or _failed(fraud):
        return "manual_review"
    package_ok = document.get("package_status") in {"accepted", "partial"}
    decision = eligibility.get("decision")
    grounded = int(eligibility.get("groundedness_score") or 0) >= 4
    critical = bool(fraud.get("critical"))
    risk = fraud.get("risk_level")
    if (
        package_ok
        and decision in _POSITIVE
        and grounded
        and not eligibility.get("numeric_inconsistency")
        and not critical
        and risk in _ACCEPTABLE_RISK
        and _disputes_cleared(eligibility, fraud)
    ):
        return "approve_candidate"
    if decision == "ineligible" and grounded and not critical:
        return "reject_candidate"
    return "manual_review"
