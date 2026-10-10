from __future__ import annotations

import json
import logging
import re
import time
from typing import Protocol

logger = logging.getLogger(__name__)


class JsonLLM(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse_json_object(text: str) -> dict:
    raw = text.strip()
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


class GeminiJsonLLM:
    def __init__(self, api_key: str, model: str = "gemini-3.8-flash") -> None:
        self.api_key = api_key
        self.model = model
        self._llm = None

    def _client(self):
        if self._llm is None:
            from langchain_google_genai import ChatGoogleGenerativeAI

            self._llm = ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=self.api_key,
                temperature=0.1,
                max_retries=2,
                timeout=60,
                thinking_budget=0,
            )
        return self._llm

    async def complete(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        started = time.perf_counter()
        logger.info("gemini call start model=%s", self.model)
        response = await self._client().ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        logger.info("gemini call done seconds=%.1f", time.perf_counter() - started)
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(str(block))
            return " ".join(p for p in parts if p)
        return str(content)
