"""Facts the settlement rules may read. The model never writes SQL."""

from __future__ import annotations

from typing import Any, Protocol


class SettlementDataPort(Protocol):
    async def load_claim(self, claim_id: str) -> dict[str, Any] | None:
        """Return one claim snapshot, or None when the claim does not exist.

        Expected keys: claim_amount, policy_status, deductible, copay_percentage,
        remaining_balance. deductible and copay_percentage may be missing; the
        rules then treat them as zero. copay_percentage is the share the
        customer pays, from 0 to 100.
        """
