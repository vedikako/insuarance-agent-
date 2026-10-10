"""Advisory narrative. This text cannot change recommended_action."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

_NARRATIVE_PROMPT = (
    "You write a short advisory note for a human insurance reviewer. "
    "The recommended action is already chosen. Do not change it and do not tell the reviewer to ignore it. "
    'Return JSON {"narrative": "..."} only.'
)


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    raw = raw_text.strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("model did not return a JSON object")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model JSON was not an object")
    return parsed


def _amount_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def template_narrative(snapshot: dict[str, Any]) -> str:
    action = str(snapshot.get("recommended_action") or "manual_review")
    sentences = [f"Recommended action is {action}."]
    eligibility = snapshot.get("eligibility") or {}
    payable = eligibility.get("payable_amount")
    if payable is not None and eligibility.get("status") != "error":
        sentences.append(f"Suggested payable amount is {_amount_text(float(payable))} INR.")
    reasons = snapshot.get("reasons") or []
    if reasons:
        sentences.append("Reasons: " + ", ".join(str(reason) for reason in reasons) + ".")
    return " ".join(sentences)


async def write_narrative(snapshot: dict[str, Any], *, llm: Any | None = None) -> str:
    fallback = template_narrative(snapshot)
    if llm is None:
        return fallback
    try:
        raw = await llm.complete(_NARRATIVE_PROMPT, fallback)
    except Exception:
        logger.exception("review narrative model failed")
        return fallback
    try:
        parsed = _parse_json_object(raw)
    except Exception:
        logger.exception("narrative JSON parse failed")
        return fallback
    narrative = str(parsed.get("narrative") or "").strip()
    return narrative or fallback
