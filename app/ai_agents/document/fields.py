from __future__ import annotations

REQUIRED_FIELDS = {
    "HOSPITAL_BILL": [
        "patient_name",
        "hospital_name",
        "admission_date",
        "discharge_date",
        "bill_total",
        "line_items",
    ],
    "DISCHARGE_SUMMARY": ["patient_name", "diagnosis"],
    "AADHAAR": ["holder_name", "id_last4"],
    "PAN": ["holder_name", "id_last4"],
    "REPAIR_ESTIMATE": ["customer_name", "workshop_name", "estimate_total"],
    "FIR": ["complainant_name", "incident_date"],
    "RC": ["owner_name", "registration_number"],
}

ID_TYPES = {"AADHAAR", "PAN"}
OPTIONAL = {"HEALTH": "DISCHARGE_SUMMARY", "MOTOR": "RC"}


def normalize_name(value: str | None) -> str:
    return " ".join(str(value or "").lower().split())


def is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False


def missing_fields(type_code: str, fields: dict) -> list[str]:
    return [name for name in REQUIRED_FIELDS.get(type_code, []) if is_blank(fields.get(name))]


def required_file_gaps(claim_type: str, type_codes: set[str]) -> list[str]:
    gaps = []
    if claim_type == "HEALTH":
        if "HOSPITAL_BILL" not in type_codes:
            gaps.append("HOSPITAL_BILL")
    else:
        if "REPAIR_ESTIMATE" not in type_codes and "FIR" not in type_codes:
            gaps.append("REPAIR_ESTIMATE_OR_FIR")
    if "AADHAAR" not in type_codes and "PAN" not in type_codes:
        gaps.append("AADHAAR_OR_PAN")
    return gaps
