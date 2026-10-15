from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.ai_agents.settlement.port import SettlementDataPort
from app.ai_agents.settlement.rules import assess_claim
from app.ai_agents.settlement.state import SettlementState

logger = logging.getLogger(__name__)


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


async def assess_node(state: SettlementState, config: RunnableConfig) -> dict[str, Any]:
    port: SettlementDataPort = _configurable(config)["deps"]
    claim_id = state["claim_id"]
    logger.info("settlement assess start claim_id=%s", claim_id)
    try:
        claim = await port.load_claim(claim_id)
    except Exception:
        logger.exception("settlement claim lookup failed")
        return {
            "error_code": "TOOL_FAILED",
            "error_message": "The claim lookup failed.",
        }
    if not claim:
        return {
            "error_code": "BAD_INPUT",
            "error_message": "Claim not found.",
        }
    assessed = assess_claim(claim)
    logger.info("settlement decision=%s payable=%s", assessed["decision"], assessed["payable_amount"])
    return {"claim": claim, **assessed}
