from __future__ import annotations


def can_read_policy(role: str, actor_id: str, owner_id: str | None) -> bool:
    if owner_id is None:
        return False
    if role in {"INSURANCE_COMPANY", "AUDITOR"}:
        return True
    return role == "CUSTOMER" and actor_id == owner_id
