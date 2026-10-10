from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.ai_agents.fraud.port import FraudDataPort
from app.ai_agents.fraud.rules import score_claim
from app.ai_agents.fraud.state import FraudState

logger = logging.getLogger(__name__)


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


async def review_node(state: FraudState, config: RunnableConfig) -> dict[str, Any]:
    port: FraudDataPort = _configurable(config)["deps"]
    claim_id = state["claim_id"]
    logger.info("fraud review start claim_id=%s", claim_id)
    try:
        claim = await port.load_claim(claim_id)
    except Exception:
        logger.exception("fraud claim lookup failed")
        return {
            "error_code": "TOOL_FAILED",
            "error_message": "The claim lookup failed.",
        }
    if not claim:
        return {
            "error_code": "BAD_INPUT",
            "error_message": "Claim not found.",
        }
    scored = score_claim(claim)
    logger.info("fraud review risk=%s flags=%s", scored["risk"], len(scored["flags"]))
    return {"claim": claim, **scored}
