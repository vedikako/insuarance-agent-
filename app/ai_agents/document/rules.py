from __future__ import annotations

from app.ai_agents.document.fields import ID_TYPES, OPTIONAL, missing_fields, normalize_name, required_file_gaps


def _confidence_ok(document: dict) -> bool:
    return float(document.get("confidence") or 0) >= 0.60


def kyc_failure(policyholder_name: str, policyholder_id_last4: str | None, documents: list[dict]) -> str:
    """Return a reason when a required id is present and does not match. Empty string means no KYC failure."""
    ids = [doc for doc in documents if doc.get("type_code") in ID_TYPES]
    if not ids:
        return ""
    expected = normalize_name(policyholder_name)
    for document in ids:
        holder = normalize_name(str((document.get("fields") or {}).get("holder_name") or ""))
        if not holder:
            continue
        if holder != expected:
            seen = (document.get("fields") or {}).get("holder_name")
            return f"{document.get('type_code')} name {seen} does not match policyholder {policyholder_name}."
        last4 = str((document.get("fields") or {}).get("id_last4") or "")
        if policyholder_id_last4 and last4 and last4 != str(policyholder_id_last4):
            return f"{document.get('type_code')} id ending {last4} does not match the policyholder."
    if all(not normalize_name(str((doc.get("fields") or {}).get("holder_name") or "")) for doc in ids):
        return ""
    return ""


def classify_package(
    *,
    claim_type: str,
    policyholder_name: str,
    policyholder_id_last4: str | None,
    uploaded_types: set[str],
    documents: list[dict],
) -> dict:
    kyc_reason = kyc_failure(policyholder_name, policyholder_id_last4, documents)
    gaps = required_file_gaps(claim_type, uploaded_types)
    field_gaps: list[str] = []
    weak_required = False
    for document in documents:
        type_code = str(document.get("type_code") or "")
        if type_code not in uploaded_types:
            continue
        missing = missing_fields(type_code, document.get("fields") or {})
        if type_code in ID_TYPES or type_code in {"HOSPITAL_BILL", "REPAIR_ESTIMATE", "FIR"}:
            field_gaps.extend(missing)
            if missing or not _confidence_ok(document):
                weak_required = True
    if kyc_reason and not gaps:
        return {
            "package_status": "kyc_failed",
            "kyc_match": False,
            "kyc_reason": kyc_reason,
            "missing_fields": field_gaps,
            "needs_manual_review": True,
        }
    if gaps or field_gaps or weak_required:
        return {
            "package_status": "needs_reupload",
            "kyc_match": not bool(kyc_reason),
            "kyc_reason": kyc_reason,
            "missing_fields": gaps + field_gaps,
            "needs_manual_review": True,
        }
    optional = OPTIONAL.get(claim_type)
    optional_docs = [doc for doc in documents if doc.get("type_code") == optional]
    optional_weak = bool(optional) and (
        not optional_docs
        or any(missing_fields(str(optional), doc.get("fields") or {}) or not _confidence_ok(doc) for doc in optional_docs)
    )
    return {
        "package_status": "partial" if optional_weak else "accepted",
        "kyc_match": True,
        "kyc_reason": "",
        "missing_fields": [],
        "needs_manual_review": False,
    }
