from __future__ import annotations

import hashlib
from datetime import date
from typing import Any


CRITICAL_FLAGS = {
    "COPY_MOVE_DETECTED",
    "PHASH_DUPLICATE",
    "ARITHMETIC_MISMATCH",
    "CLAIM_BEFORE_POLICY",
    "CONTENT_CRITICAL",
    "ID_MISMATCH",
}

EARLY_CLAIM_DAYS = 7

FLAG_TOOL = {
    "ARITHMETIC_MISMATCH": "arithmetic_gst",
    "GST_MISMATCH": "arithmetic_gst",
    "PHASH_DUPLICATE": "image_forensics",
    "COPY_MOVE_DETECTED": "image_forensics",
    "REDACTION_SUSPECT": "id_redaction",
    "ID_MISMATCH": "cross_document",
    "CROSS_DOC_NAME_MISMATCH": "cross_document",
    "CROSS_DOC_AMOUNT_MISMATCH": "cross_document",
    "CROSS_DOC_DATE_MISMATCH": "cross_document",
    "CLAIM_BEFORE_POLICY": "cross_document",
    "VERY_EARLY_CLAIM": "behavioral",
    "BEHAVIORAL_VELOCITY": "behavioral",
    "CONTENT_CRITICAL": "content_review",
    "DIAGNOSIS_CONFLICT": "content_review",
}


def money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def name(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def line_total(fields: dict[str, Any]) -> float | None:
    items = fields.get("line_items")
    if not isinstance(items, list) or not items:
        return None
    total = 0.0
    seen = False
    for item in items:
        amount = money(item.get("amount") if isinstance(item, dict) else item)
        if amount is None:
            continue
        total += amount
        seen = True
    return round(total, 2) if seen else None


def parse_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def differs(left: float, right: float) -> bool:
    return abs(left - right) > 1


def result(tool_name: str, *, ok: bool, score: float, flags: list[str], evidence: str, details: dict | None = None) -> dict:
    return {
        "tool_name": tool_name,
        "ok": ok,
        "score": max(0.0, min(float(score), 1.0)),
        "flags": flags,
        "evidence": evidence,
        "details": details or {},
    }


def documents_of(request: dict[str, Any], *type_codes: str) -> list[dict[str, Any]]:
    found = []
    for document in request.get("documents") or []:
        if not type_codes or document.get("type_code") in type_codes:
            found.append(document)
    return found


def arithmetic_gst(request: dict[str, Any]) -> dict:
    flags: list[str] = []
    notes: list[str] = []
    claim_amount = money(request.get("claim_amount"))
    for document in documents_of(request, "HOSPITAL_BILL", "REPAIR_ESTIMATE"):
        fields = document.get("fields") or {}
        billed = money(fields.get("bill_total"))
        if billed is None:
            billed = money(fields.get("estimate_total"))
        items = line_total(fields)
        if billed is not None and items is not None and differs(items, billed):
            flags.append("ARITHMETIC_MISMATCH")
            notes.append(f"Line items sum to {items:.0f}.")
        if billed is not None and claim_amount is not None and differs(billed, claim_amount):
            flags.append("ARITHMETIC_MISMATCH")
            notes.append(f"The bill total {billed:.0f} does not match the claim amount {claim_amount:.0f}.")
        taxable = money(fields.get("taxable_amount"))
        gst = money(fields.get("gst_amount"))
        if taxable is not None and gst is not None and differs(gst, round(taxable * 0.18, 2)):
            flags.append("GST_MISMATCH")
            notes.append("The GST amount does not match 18 percent of the taxable amount.")
    flags = list(dict.fromkeys(flags))
    if not documents_of(request, "HOSPITAL_BILL", "REPAIR_ESTIMATE"):
        return result("arithmetic_gst", ok=True, score=0, flags=[], evidence="No bill or estimate was attached.")
    score = 0.75 if "ARITHMETIC_MISMATCH" in flags else (0.35 if flags else 0)
    evidence = " ".join(notes) if notes else "The bill amounts match."
    return result("arithmetic_gst", ok=True, score=score, flags=flags, evidence=evidence)


def cross_document(request: dict[str, Any]) -> dict:
    flags: list[str] = []
    notes: list[str] = []
    policy = request.get("policy") or {}
    holder = name(policy.get("holder_name"))
    start = parse_day(policy.get("start_date"))
    end = parse_day(policy.get("end_date"))
    names = []
    amounts = []
    days = []
    for document in request.get("documents") or []:
        fields = document.get("fields") or {}
        for key in ("patient_name", "customer_name", "holder_name", "complainant_name", "owner_name"):
            if fields.get(key):
                names.append((key, name(fields.get(key)), str(fields.get(key))))
        for key in ("bill_total", "estimate_total"):
            amount = money(fields.get(key))
            if amount is not None:
                amounts.append(amount)
        for key in ("admission_date", "discharge_date", "incident_date"):
            day = parse_day(fields.get(key))
            if day is not None:
                days.append(day)
    incident = parse_day(request.get("incident_date"))
    if incident is not None:
        days.append(incident)
    if holder:
        for key, normalized, raw in names:
            if normalized != holder:
                flag = "ID_MISMATCH" if key == "holder_name" else "CROSS_DOC_NAME_MISMATCH"
                flags.append(flag)
                notes.append(f"The {key} {raw} does not match the policyholder.")
    if len(set(amounts)) > 1:
        flags.append("CROSS_DOC_AMOUNT_MISMATCH")
        notes.append("Documents on this claim do not show the same amount.")
    outside = [day for day in days if (start and day < start) or (end and day > end)]
    if any(start and day < start for day in days):
        flags.append("CLAIM_BEFORE_POLICY")
        notes.append("A claim date is before the policy start date.")
    elif outside:
        flags.append("CROSS_DOC_DATE_MISMATCH")
        notes.append("A claim date is outside the policy window.")
    flags = list(dict.fromkeys(flags))
    score = 0.85 if "CLAIM_BEFORE_POLICY" in flags or "ID_MISMATCH" in flags else (0.4 if flags else 0)
    evidence = " ".join(notes) if notes else "Names, amounts, and dates agree across the documents."
    return result("cross_document", ok=True, score=score, flags=flags, evidence=evidence)


async def image_forensics(request: dict[str, Any], reader: Any) -> dict:
    flags: list[str] = []
    checked = 0
    for document in request.get("documents") or []:
        mime = str(document.get("mime") or "")
        if mime not in {"application/pdf", "image/png", "image/jpeg"}:
            continue
        fields = document.get("fields") or {}
        known = {str(item) for item in fields.get("known_hashes") or []}
        try:
            data = await reader(document.get("storage_uri") or "")
        except Exception:
            continue
        checked += 1
        digest = hashlib.sha256(data).hexdigest()
        if digest in known or any(str(item) == digest for item in known):
            flags.append("PHASH_DUPLICATE")
    if checked == 0:
        return result(
            "image_forensics",
            ok=True,
            score=0,
            flags=[],
            evidence="File bytes were not available. No pixel check was run.",
        )
    if flags:
        return result(
            "image_forensics",
            ok=True,
            score=0.9,
            flags=["PHASH_DUPLICATE"],
            evidence="The file hash matches a known duplicate.",
        )
    return result("image_forensics", ok=True, score=0, flags=[], evidence="No duplicate hash was found.")


def id_redaction(request: dict[str, Any]) -> dict:
    flags: list[str] = []
    for document in documents_of(request, "AADHAAR", "PAN"):
        fields = document.get("fields") or {}
        for key, value in fields.items():
            digits = "".join(ch for ch in str(value) if ch.isdigit())
            if key != "id_last4" and len(digits) > 4:
                flags.append("REDACTION_SUSPECT")
    flags = list(dict.fromkeys(flags))
    evidence = "A full identity number is still visible." if flags else "Identity numbers are reduced to the last four digits."
    return result("id_redaction", ok=True, score=0.3 if flags else 0, flags=flags, evidence=evidence)


def early_claim(request: dict[str, Any]) -> bool:
    policy = request.get("policy") or {}
    start = parse_day(policy.get("start_date"))
    incident = parse_day(request.get("incident_date"))
    if start is None or incident is None:
        return False
    gap = (incident - start).days
    return 0 <= gap <= EARLY_CLAIM_DAYS


def behavioral(request: dict[str, Any]) -> dict:
    behavior = request.get("behavior") or {}
    recent = int(behavior.get("recent_claims_30d") or 0)
    prior = int(behavior.get("prior_fraud_flags") or 0)
    flags: list[str] = []
    notes: list[str] = []
    if recent >= 3:
        flags.append("BEHAVIORAL_VELOCITY")
        notes.append(f"{recent} other claims were filed in the last 30 days.")
    if prior > 0:
        if "BEHAVIORAL_VELOCITY" not in flags:
            flags.append("BEHAVIORAL_VELOCITY")
        notes.append("This policy already has prior fraud flags.")
    if early_claim(request):
        flags.append("VERY_EARLY_CLAIM")
        notes.append(f"The incident date is within {EARLY_CLAIM_DAYS} days of the policy start.")
    evidence = " ".join(notes) if notes else "Recent claim activity is within a normal range."
    return result("behavioral", ok=True, score=0.45 if flags else 0, flags=flags, evidence=evidence)


def content_review(request: dict[str, Any]) -> dict:
    flags: list[str] = []
    discharges = documents_of(request, "DISCHARGE_SUMMARY")
    claim_text = " ".join(
        str((document.get("fields") or {}).get("diagnosis") or "")
        for document in request.get("documents") or []
        if document.get("type_code") != "DISCHARGE_SUMMARY"
    ).strip()
    for document in discharges:
        diagnosis = str((document.get("fields") or {}).get("diagnosis") or "").strip()
        if not diagnosis and claim_text:
            flags.append("CONTENT_CRITICAL")
    evidence = (
        "The discharge summary has no diagnosis while the claim names a condition."
        if flags
        else "The diagnosis text does not contradict the other documents."
    )
    return result("content_review", ok=True, score=0.8 if flags else 0, flags=flags, evidence=evidence)


TOOLS = {
    "arithmetic_gst": arithmetic_gst,
    "cross_document": cross_document,
    "image_forensics": image_forensics,
    "id_redaction": id_redaction,
    "behavioral": behavioral,
    "content_review": content_review,
}


def disputes_for(request: dict[str, Any]) -> list[dict[str, str]]:
    disputes: list[dict[str, str]] = []
    claim_amount = money(request.get("claim_amount"))
    policy = request.get("policy") or {}
    holder = name(policy.get("holder_name"))
    start = parse_day(policy.get("start_date"))
    end = parse_day(policy.get("end_date"))
    for document in documents_of(request, "HOSPITAL_BILL", "REPAIR_ESTIMATE"):
        fields = document.get("fields") or {}
        billed = money(fields.get("bill_total"))
        if billed is None:
            billed = money(fields.get("estimate_total"))
        items = line_total(fields)
        field_name = "bill_total" if fields.get("bill_total") is not None else "estimate_total"
        if items is not None and billed is not None and differs(items, billed):
            disputes.append(
                {
                    "code": "AMOUNT_CONFLICT",
                    "field": field_name,
                    "fraud_value": f"{(billed if billed is not None else claim_amount):.0f}",
                    "document_value": f"{items:.0f}",
                    "detail": f"Line items sum to {items:.0f}.",
                }
            )
        elif billed is not None and claim_amount is not None and differs(billed, claim_amount):
            disputes.append(
                {
                    "code": "AMOUNT_CONFLICT",
                    "field": field_name,
                    "fraud_value": f"{claim_amount:.0f}",
                    "document_value": f"{billed:.0f}",
                    "detail": f"The bill total differs from the claim amount by more than 1.",
                }
            )
        for key in ("patient_name", "customer_name"):
            seen = fields.get(key)
            if holder and seen and name(seen) != holder:
                disputes.append(
                    {
                        "code": "IDENTITY_CONFLICT",
                        "field": key,
                        "fraud_value": str(policy.get("holder_name") or ""),
                        "document_value": str(seen),
                        "detail": "The name on the bill does not match the policyholder.",
                    }
                )
    days = []
    for document in request.get("documents") or []:
        fields = document.get("fields") or {}
        for key in ("admission_date", "discharge_date", "incident_date"):
            day = parse_day(fields.get(key))
            if day is not None:
                days.append((key, day))
    incident = parse_day(request.get("incident_date"))
    if incident is not None:
        days.append(("incident_date", incident))
    for key, day in days:
        if (start and day < start) or (end and day > end):
            disputes.append(
                {
                    "code": "DATE_CONFLICT",
                    "field": key,
                    "fraud_value": str(policy.get("start_date") or ""),
                    "document_value": day.isoformat(),
                    "detail": "A claim date falls outside the policy window.",
                }
            )
            break
    discharges = documents_of(request, "DISCHARGE_SUMMARY")
    other_diagnosis = " ".join(
        str((document.get("fields") or {}).get("diagnosis") or "")
        for document in request.get("documents") or []
        if document.get("type_code") != "DISCHARGE_SUMMARY"
    ).strip()
    for document in discharges:
        diagnosis = str((document.get("fields") or {}).get("diagnosis") or "").strip()
        if not diagnosis and other_diagnosis:
            disputes.append(
                {
                    "code": "DIAGNOSIS_CONFLICT",
                    "field": "diagnosis",
                    "fraud_value": other_diagnosis[:80],
                    "document_value": "",
                    "detail": "The discharge diagnosis is empty while another document names a condition.",
                }
            )
    return disputes
