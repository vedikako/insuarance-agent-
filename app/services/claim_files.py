"""Save a customer upload where the document agent can read it later."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

_ROOT = Path(__file__).resolve().parents[2] / "data" / "uploads"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MIME = {
    "application/pdf": "application/pdf",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
}
DOCUMENT_TYPES = {
    "HOSPITAL_BILL",
    "DISCHARGE_SUMMARY",
    "AADHAAR",
    "PAN",
    "REPAIR_ESTIMATE",
    "FIR",
    "RC",
}


def document_mime(content_type: str | None, filename: str) -> str | None:
    raw = (content_type or "").split(";")[0].strip().lower()
    if raw in _MIME:
        return _MIME[raw]
    name = filename.lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return None


def safe_filename(filename: str | None, fallback: str) -> str:
    name = Path(filename or "").name
    cleaned = _SAFE.sub("-", name).strip(".-")
    return cleaned or fallback


def save_upload(claim_id: str, filename: str, data: bytes) -> tuple[str, str]:
    document_id = str(uuid4())
    folder = _ROOT / claim_id / document_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(data)
    return document_id, str(path)
