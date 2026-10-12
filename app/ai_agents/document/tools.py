from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from app.ai_agents.document.fields import missing_fields
from app.ai_agents.document.prompts import EXTRACT_PROMPT

logger = logging.getLogger(__name__)


class JsonLLM(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


def _parse_json_object(raw_text: str) -> dict[str, Any] | None:
    raw = raw_text.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def extract_document(
    data: bytes,
    *,
    type_code: str,
    focus_fields: list[str] | None,
    llm: JsonLLM | None,
) -> tuple[dict[str, Any], float]:
    parsed = _parse_json_object(data.decode("utf-8", errors="ignore"))
    if parsed is None and llm is not None:
        focus = ", ".join(focus_fields or [])
        user = f"Document type: {type_code}\nFields: {focus or 'all required fields'}\n\n{data.decode('utf-8', errors='ignore')[:8000]}"
        try:
            parsed = _parse_json_object(await llm.complete(EXTRACT_PROMPT, user))
        except Exception:
            logger.exception("document extract model failed")
            parsed = None
    if not parsed:
        return {}, 0.2
    confidence = parsed.pop("confidence", None)
    fields = {key: value for key, value in parsed.items() if key != "type_code"}
    if focus_fields:
        focused = {key: fields.get(key) for key in focus_fields}
        for key, value in fields.items():
            focused.setdefault(key, value)
        fields = focused
    missing = missing_fields(type_code, fields)
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        score = float(confidence)
    else:
        score = 0.95 if not missing else 0.45
    return fields, max(0.0, min(score, 1.0))
