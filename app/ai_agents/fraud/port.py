"""Facts the fraud rules may read. The model never writes SQL."""

from __future__ import annotations

from typing import Any, Protocol


class FraudDataPort(Protocol):
    async def load_claim(self, claim_id: str) -> dict[str, Any] | None:
        """Return one claim snapshot, or None when the claim does not exist.

        Expected keys: claim_amount, approved_amount, policy_status, sum_insured,
        remaining_balance, other_claim_amounts. other_claim_amounts lists amounts
        on the same policy, excluding this claim.
        """
