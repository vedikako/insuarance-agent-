JUDGE_PROMPT = """You score how well the draft is supported by the quoted clauses.
Return JSON {"groundedness_score": 1-5, "unsupported_statements": [], "judge_rationale": "one sentence"}.
Score 5 only when every amount and cover statement is in the clauses or the ledger snapshot.
"""

DRAFT_PROMPT = """Decide whether this claim is covered by the quoted clauses and the ledger.
Return JSON {"decision": "eligible|partially_eligible|ineligible|insufficient_evidence", "payable_amount": number, "rationale": "one sentence"}.
Do not exceed the remaining balance. Do not invent a clause.
"""
