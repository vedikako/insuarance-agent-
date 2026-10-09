from __future__ import annotations

from typing import Any

from app.services.policy_data import PolicyDataPort

TOOL_NAMES = (
    "get_balance",
    "list_utilization_entries",
    "get_policy_facts",
    "retrieve_clauses",
    "list_claims_for_policy",
)

MAX_TOOL_CALLS = 4


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict, str)) and len(value) == 0:
        return True
    return False


async def execute_tool(
    name: str,
    port: PolicyDataPort,
    policy_id: str,
    query: str,
) -> tuple[Any, dict[str, Any]]:
    if name not in TOOL_NAMES:
        return {"error": "unknown tool"}, {
            "tool_name": name,
            "ok": False,
            "summary": "That tool is not in the policy catalog.",
        }
    try:
        if name == "get_balance":
            payload = await port.get_balance(policy_id)
            summary = "Read sum insured, amount used, and amount left from the ledger."
        elif name == "list_utilization_entries":
            payload = await port.list_utilization_entries(policy_id)
            summary = "Read recent ledger rows for this policy."
        elif name == "get_policy_facts":
            payload = await port.get_policy_facts(policy_id)
            summary = "Read policy number, dates, premium, and deductible from SQL."
        elif name == "retrieve_clauses":
            payload = await port.retrieve_clauses(policy_id, query, k=4)
            summary = "Retrieved policy wording for this question."
        else:
            payload = await port.list_claims_for_policy(policy_id)
            summary = "Read claim numbers, statuses, and amounts for this policy."
    except Exception as exc:
        return {"error": str(exc)}, {
            "tool_name": name,
            "ok": False,
            "summary": "The tool failed before it could return data.",
        }
    ok = not is_empty(payload) and not (isinstance(payload, dict) and payload.get("error"))
    if not ok:
        summary = "The tool returned no rows."
    return payload, {"tool_name": name, "ok": ok, "summary": summary}
