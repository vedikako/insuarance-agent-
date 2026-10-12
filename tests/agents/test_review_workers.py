import asyncio
import json

from app.ai_agents.contracts.messages import (
    DocumentAgentRequest,
    DocumentRef,
    DocumentRevision,
    EligibilityAgentRequest,
    EligibilityRevision,
    FraudAgentRequest,
    FraudBehavior,
    FraudDocument,
    FraudPolicy,
    FraudRevision,
    LedgerSnapshot,
    ReviewRequest,
)
from app.ai_agents.document.runner import run_document_agent
from app.ai_agents.eligibility.runner import run_eligibility_agent
from app.ai_agents.fraud.runner import run_fraud_agent
from app.ai_agents.orchestrator.runner import context_from_rows, run_review
from app.services.review_inputs import register_document_bytes, register_policy_clauses, reset_review_inputs

POLICY = FraudPolicy(
    start_date="2026-01-01",
    end_date="2026-12-31",
    sum_insured=500000,
    holder_name="Asha Rao",
)
BEHAVIOR = FraudBehavior(recent_claims_30d=0, total_claim_amount_90d=0, prior_fraud_flags=0)
CLAUSE = {
    "chunk_id": "clause-1",
    "section_path": "4.1 Hospitalisation",
    "content": "Hospitalisation is covered as an inpatient procedure.",
}


def _bill(**fields) -> dict:
    payload = {
        "patient_name": "Asha Rao",
        "hospital_name": "City Hospital",
        "admission_date": "2026-06-01",
        "discharge_date": "2026-06-05",
        "bill_total": 10000,
        "line_items": [{"description": "Room", "amount": 10000}],
    }
    payload.update(fields)
    return payload


def _fraud_request(bill: dict, *, attempt: int = 1, revision: FraudRevision | None = None) -> FraudAgentRequest:
    return FraudAgentRequest(
        claim_id="c1",
        claim_type="HEALTH",
        claim_amount=18000 if bill.get("bill_total") == 18000 else 10000,
        incident_date="2026-06-01",
        policy=POLICY,
        documents=[
            FraudDocument(
                document_id="bill-1",
                type_code="HOSPITAL_BILL",
                mime="application/pdf",
                storage_uri="mem://bill",
                fields=bill,
            )
        ],
        behavior=BEHAVIOR,
        attempt=attempt,  # type: ignore[arg-type]
        revision=revision,
    )


def test_line_items_that_do_not_match_are_critical():
    bill = _bill(bill_total=18000, line_items=[{"description": "Room", "amount": 12000}])
    result = asyncio.run(run_fraud_agent(_fraud_request(bill)))
    assert result.status == "ok"
    assert result.critical is True
    assert "ARITHMETIC_MISMATCH" in result.flags
    assert result.risk_level in {"HIGH", "VERY_HIGH"}
    assert result.disputes[0].code == "AMOUNT_CONFLICT"
    assert result.disputes[0].detail == "Line items sum to 12000."
    assert result.needs_manual_review is True


def test_a_matching_bill_is_not_critical():
    result = asyncio.run(run_fraud_agent(_fraud_request(_bill())))
    assert result.status == "ok"
    assert result.critical is False
    assert result.disputes == []
    assert result.risk_level == "MINIMAL"
    assert "arithmetic_gst" in result.tools_used


def test_fraud_attempt_three_is_bad_input():
    request = FraudAgentRequest.model_construct(
        claim_id="c1",
        claim_type="HEALTH",
        claim_amount=10000,
        incident_date=None,
        policy=POLICY,
        documents=[],
        behavior=BEHAVIOR,
        attempt=3,
        revision=None,
    )
    result = asyncio.run(run_fraud_agent(request))
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "BAD_INPUT"


def test_fraud_revision_reruns_only_the_flagged_tool():
    bill = _bill(bill_total=18000, line_items=[{"description": "Room", "amount": 12000}])
    request = _fraud_request(
        bill,
        attempt=2,
        revision=FraudRevision(
            reason="high risk",
            flag="ARITHMETIC_MISMATCH",
            question="Cite the evidence for this flag in one sentence.",
        ),
    )
    result = asyncio.run(run_fraud_agent(request))
    assert result.tools_used == ["arithmetic_gst"]
    assert result.explanation.startswith("Cite the evidence for this flag in one sentence.")
    assert result.critical is True


def _document_request(documents: list[DocumentRef], *, attempt: int = 1) -> DocumentAgentRequest:
    return DocumentAgentRequest(
        claim_id="c1",
        claim_type="HEALTH",
        attempt=attempt,  # type: ignore[arg-type]
        policyholder_name="Asha Rao",
        policyholder_id_last4="1234",
        documents=documents,
        revision=None if attempt == 1 else DocumentRevision(reason="missing fields", focus_fields=["hospital_name"]),
    )


def _refs() -> list[DocumentRef]:
    return [
        DocumentRef(
            document_id="bill-1",
            type_code="HOSPITAL_BILL",
            mime="application/pdf",
            storage_uri="mem://bill",
            filename="bill.pdf",
        ),
        DocumentRef(
            document_id="id-1",
            type_code="AADHAAR",
            mime="image/jpeg",
            storage_uri="mem://id",
            filename="aadhaar.jpg",
        ),
        DocumentRef(
            document_id="discharge-1",
            type_code="DISCHARGE_SUMMARY",
            mime="application/pdf",
            storage_uri="mem://discharge",
            filename="discharge.pdf",
        ),
    ]


def test_document_package_is_accepted_when_the_id_matches():
    reset_review_inputs()
    register_document_bytes("mem://bill", json.dumps(_bill()).encode())
    register_document_bytes(
        "mem://id",
        json.dumps({"holder_name": "asha  rao", "id_last4": "1234"}).encode(),
    )
    register_document_bytes(
        "mem://discharge",
        json.dumps({"patient_name": "Asha Rao", "diagnosis": "Fever"}).encode(),
    )
    result = asyncio.run(run_document_agent(_document_request(_refs())))
    assert result.status == "ok"
    assert result.package_status == "accepted"
    assert result.kyc_match is True
    assert result.documents[0].fields["bill_total"] == 10000
    assert all(call.tool_name != "reextract" for call in result.tool_calls)


def test_document_kyc_failure_stops_the_package():
    reset_review_inputs()
    register_document_bytes("mem://bill", json.dumps(_bill()).encode())
    register_document_bytes("mem://id", json.dumps({"holder_name": "Ravi Shah", "id_last4": "1234"}).encode())
    register_document_bytes(
        "mem://discharge",
        json.dumps({"patient_name": "Asha Rao", "diagnosis": "Fever"}).encode(),
    )
    result = asyncio.run(run_document_agent(_document_request(_refs())))
    assert result.package_status == "kyc_failed"
    assert result.needs_manual_review is True
    assert "Ravi Shah" in result.kyc_reason


def test_missing_id_asks_for_reupload_after_one_reextract():
    reset_review_inputs()
    register_document_bytes("mem://bill", json.dumps(_bill(hospital_name="")).encode())
    result = asyncio.run(run_document_agent(_document_request(_refs()[:1])))
    assert result.package_status == "needs_reupload"
    assert any(call.tool_name == "reextract" for call in result.tool_calls)
    assert "AADHAAR_OR_PAN" in result.missing_fields


class _HighPayable:
    async def complete(self, system: str, user: str) -> str:
        return '{"decision": "eligible", "payable_amount": 999999, "rationale": "Pay the full bill."}'


def _eligibility(fields: dict, *, attempt: int = 1, revision: EligibilityRevision | None = None, claim_amount: float = 10000):
    return EligibilityAgentRequest(
        claim_id="c1",
        policy_id="p1",
        claim_type="HEALTH",
        claim_amount=claim_amount,
        incident_date="2026-06-01",
        diagnosis="Fever",
        incident_summary="Hospital stay",
        extracted_fields=fields,
        ledger=LedgerSnapshot(sum_insured=500000, remaining_balance=480000, amount_already_paid=20000),
        attempt=attempt,  # type: ignore[arg-type]
        revision=revision,
    )


def test_eligibility_without_a_clause_is_not_a_cover_decision():
    async def no_clauses(policy_id: str, query: str):
        calls.append(query)
        return []

    calls: list[str] = []
    result = asyncio.run(run_eligibility_agent(_eligibility(_bill()), clauses=no_clauses))
    assert result.decision == "insufficient_evidence"
    assert "NO_CITATION" in result.manual_review_reasons
    assert len(calls) == 2
    assert result.payable_amount == 0


def test_eligibility_clamps_a_payable_above_the_ledger():
    async def clauses(policy_id: str, query: str):
        return [CLAUSE]

    result = asyncio.run(
        run_eligibility_agent(
            _eligibility({"diagnosis": "Fever"}, claim_amount=500000),
            clauses=clauses,
            llm=_HighPayable(),
        )
    )
    assert result.payable_amount == 480000
    assert result.numeric_inconsistency is True
    assert "PAYABLE_ABOVE_LEDGER" in result.manual_review_reasons
    assert result.citations[0].chunk_id == "clause-1"


def test_eligibility_revises_a_disputed_amount():
    async def clauses(policy_id: str, query: str):
        return [CLAUSE]

    revision = EligibilityRevision(
        reason="fraud dispute",
        disputes=[
            {
                "code": "AMOUNT_CONFLICT",
                "field": "bill_total",
                "fraud_value": "18000",
                "document_value": "12000",
                "detail": "Line items sum to 12000.",
            }
        ],
    )
    result = asyncio.run(
        run_eligibility_agent(
            _eligibility(_bill(bill_total=18000, line_items=[{"description": "Room", "amount": 18000}]), attempt=2, revision=revision, claim_amount=18000),
            clauses=clauses,
        )
    )
    assert len(result.answered_disputes) == 1
    assert result.answered_disputes[0].code == "AMOUNT_CONFLICT"
    assert result.answered_disputes[0].stance == "revise"
    assert result.payable_amount == 12000
    assert result.decision == "partially_eligible"


def test_one_clean_claim_goes_through_the_orchestrator():
    reset_review_inputs()
    register_document_bytes("mem://bill", json.dumps(_bill()).encode())
    register_document_bytes("mem://id", json.dumps({"holder_name": "Asha Rao", "id_last4": "1234"}).encode())
    register_document_bytes("mem://discharge", json.dumps({"patient_name": "Asha Rao", "diagnosis": "Fever"}).encode())
    register_policy_clauses("p1", [CLAUSE])
    context = context_from_rows(
        claim_id="c1",
        policy_id="p1",
        claim_amount=10000,
        policy_type="HEALTH",
        start_date="2026-01-01",
        end_date="2026-12-31",
        sum_insured=500000,
        insured_name="Asha Rao",
        utilized=0,
        documents=[item.model_dump(mode="json") for item in _refs()],
        diagnosis="Fever",
        incident_summary="Hospital stay",
        incident_date="2026-06-01",
        policyholder_id_last4="1234",
    )
    result = asyncio.run(
        run_review(
            ReviewRequest(claim_id="c1", user_id="adjuster-1", role="INSURANCE_COMPANY"),
            context=context,
            use_model=False,
        )
    )
    assert result.run_status == "ready"
    assert result.document is not None
    assert result.document.package_status == "accepted"
    assert result.eligibility is not None
    assert result.eligibility.decision == "eligible"
    assert result.eligibility.payable_amount == 10000
    assert result.fraud is not None
    assert result.fraud.critical is False
    assert result.recommended_action == "approve_candidate"


def test_a_bill_that_does_not_add_up_stays_with_the_reviewer():
    reset_review_inputs()
    bill = _bill(bill_total=18000, line_items=[{"description": "Room", "amount": 12000}])
    register_document_bytes("mem://bill", json.dumps(bill).encode())
    register_document_bytes("mem://id", json.dumps({"holder_name": "Asha Rao", "id_last4": "1234"}).encode())
    register_document_bytes("mem://discharge", json.dumps({"patient_name": "Asha Rao", "diagnosis": "Fever"}).encode())
    register_policy_clauses("p1", [CLAUSE])
    context = context_from_rows(
        claim_id="c-mismatch",
        policy_id="p1",
        claim_amount=18000,
        policy_type="HEALTH",
        start_date="2026-01-01",
        end_date="2026-12-31",
        sum_insured=500000,
        insured_name="Asha Rao",
        utilized=0,
        documents=[item.model_dump(mode="json") for item in _refs()],
        diagnosis="Fever",
        incident_summary="Hospital stay",
        incident_date="2026-06-01",
        policyholder_id_last4="1234",
    )
    result = asyncio.run(
        run_review(
            ReviewRequest(claim_id="c-mismatch", user_id="adjuster-1"),
            context=context,
            use_model=False,
        )
    )
    assert result.fraud is not None
    assert result.fraud.critical is True
    assert result.eligibility is not None
    assert result.eligibility.payable_amount == 12000
    assert result.recommended_action == "manual_review"
    assert "CONFLICT_UNRESOLVED" in result.reasons
