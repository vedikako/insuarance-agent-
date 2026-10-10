import asyncio

from app.ai_agents.contracts.messages import (
    Dispute,
    DisputeAnswer,
    DocumentAgentResult,
    DocumentRef,
    EligibilityAgentResult,
    ExtractedDocument,
    FraudAgentResult,
    ReviewRequest,
)
from app.ai_agents.orchestrator.policy import next_action, recommended_action
from app.ai_agents.orchestrator.runner import context_from_rows, run_review
from app.ai_agents.orchestrator.trace_store import MemoryTraceStore
from tests.fakes.fake_document import scripted_document
from tests.fakes.fake_eligibility import scripted_eligibility
from tests.fakes.fake_fraud import scripted_fraud

BILL = DocumentRef(
    document_id="bill-1",
    type_code="HOSPITAL_BILL",
    mime="application/pdf",
    storage_uri="s3://claims/bill-1.pdf",
    filename="bill.pdf",
)
AMOUNT_DISPUTE = Dispute(
    code="AMOUNT_CONFLICT",
    field="bill_total",
    fraud_value="18000",
    document_value="12000",
    detail="Line items sum to 12000.",
)


def _context(**overrides) -> dict:
    context = context_from_rows(
        claim_id="c1",
        policy_id="p1",
        claim_amount=18000,
        policy_type="HEALTH",
        start_date="2026-01-01",
        end_date="2026-12-31",
        sum_insured=500000,
        insured_name="Asha Rao",
        utilized=0,
        documents=[BILL.model_dump(mode="json")],
        diagnosis="Fever",
        incident_summary="Hospital stay",
        incident_date="2026-01-15",
        policyholder_id_last4="1234",
    )
    context.update(overrides)
    return context


def _document(package_status="accepted", bill_total=18000, missing=None, attempt=1) -> DocumentAgentResult:
    return DocumentAgentResult(
        status="ok",
        attempt=attempt,
        package_status=package_status,
        documents=[
            ExtractedDocument(
                document_id="bill-1",
                type_code="HOSPITAL_BILL",
                fields={"patient_name": "Asha Rao", "bill_total": bill_total},
                confidence=0.91,
                missing_fields=list(missing or []),
            )
        ],
        missing_fields=list(missing or []),
        kyc_match=package_status != "kyc_failed",
        kyc_reason="" if package_status != "kyc_failed" else "Aadhaar name Ravi Shah does not match policyholder Asha Rao",
        needs_manual_review=package_status in {"needs_reupload", "kyc_failed"},
    )


def _eligibility(
    decision="eligible",
    payable=18000,
    groundedness=5,
    attempt=1,
    disputes=None,
    unsupported=None,
) -> EligibilityAgentResult:
    answers = []
    for dispute in disputes or []:
        answers.append(
            DisputeAnswer(
                code=dispute.code,
                stance="revise",
                revised_payable_amount=payable,
                rationale="The line items support the lower amount.",
            )
        )
    return EligibilityAgentResult(
        status="ok",
        attempt=attempt,
        decision=decision,
        payable_amount=payable,
        groundedness_score=groundedness,
        unsupported_statements=list(unsupported or []),
        answered_disputes=answers,
        numeric_inconsistency=False,
        needs_manual_review=groundedness < 4,
    )


def _fraud(critical=False, risk="MINIMAL", disputes=None, flags=None, attempt=1) -> FraudAgentResult:
    return FraudAgentResult(
        status="ok",
        attempt=attempt,
        score=0.82 if critical else 0.12,
        risk_level=risk,
        flags=list(flags or []),
        critical=critical,
        disputes=list(disputes or []),
        needs_manual_review=critical,
        explanation="Checked the bill.",
    )


def _workers(document, eligibility, fraud) -> dict:
    return {"document": document, "eligibility": eligibility, "fraud": fraud}


def _run(document, eligibility, fraud, **kwargs):
    return asyncio.run(
        run_review(
            ReviewRequest(claim_id="c1", user_id="adjuster-1", role="INSURANCE_COMPANY"),
            context=kwargs.pop("context", _context()),
            workers=_workers(document, eligibility, fraud),
            use_model=False,
            **kwargs,
        )
    )


def test_loaded_context_keeps_the_remaining_balance():
    context = context_from_rows(
        claim_id="c1",
        policy_id="p1",
        claim_amount=18000,
        policy_type="health",
        start_date="2026-01-01",
        end_date="2026-12-31",
        sum_insured=500000,
        insured_name="Asha Rao",
        utilized=20000,
    )
    assert context["claim_type"] == "HEALTH"
    assert context["ledger"]["remaining_balance"] == 480000
    assert context["ledger"]["amount_already_paid"] == 20000


def test_first_action_is_document_even_if_the_proposal_says_finish():
    async def always_finish(_state):
        return "FINISH"

    document = scripted_document([_document()])
    eligibility = scripted_eligibility([_eligibility()])
    fraud = scripted_fraud([_fraud()])
    result = _run(document, eligibility, fraud, proposer=always_finish)
    assert document.calls
    assert result.recommended_action == "approve_candidate"
    assert any(event.kind == "plan_rejected" for event in result.trace)
    plans = [event for event in result.trace if event.kind == "plan"]
    assert plans[0].payload["action"] == "CALL_DOCUMENT"


def test_kyc_failed_never_calls_fraud_or_eligibility():
    document = scripted_document([_document(package_status="kyc_failed")])
    eligibility = scripted_eligibility([_eligibility()])
    fraud = scripted_fraud([_fraud()])
    result = _run(document, eligibility, fraud)
    assert fraud.calls == []
    assert eligibility.calls == []
    assert result.eligibility is None
    assert result.fraud is None
    assert result.run_status == "escalated"
    assert result.recommended_action == "manual_review"
    assert result.reasons == ["KYC_FAILED"]
    involved = {event.from_agent for event in result.trace} | {event.to_agent for event in result.trace}
    assert "eligibility" not in involved
    assert "fraud" not in involved


def test_amount_conflict_calls_eligibility_once_more_and_stays_manual():
    document = scripted_document([_document()])
    eligibility = scripted_eligibility(
        [
            _eligibility(),
            _eligibility(decision="partially_eligible", payable=12000, attempt=2, disputes=[AMOUNT_DISPUTE]),
        ]
    )
    fraud = scripted_fraud(
        [_fraud(critical=True, risk="HIGH", disputes=[AMOUNT_DISPUTE], flags=["ARITHMETIC_MISMATCH"])]
    )
    result = _run(document, eligibility, fraud)
    assert len(eligibility.calls) == 2
    assert len(fraud.calls) == 1
    assert eligibility.calls[0].revision is None
    assert eligibility.calls[1].attempt == 2
    assert eligibility.calls[1].revision is not None
    assert eligibility.calls[1].revision.disputes[0].code == "AMOUNT_CONFLICT"
    assert "fraud" not in eligibility.calls[1].model_dump()
    assert "eligibility" not in fraud.calls[0].model_dump()
    kinds = [event.kind for event in result.trace]
    assert "critique" in kinds
    assert "revision" in kinds
    assert result.recommended_action == "manual_review"
    assert result.reasons == ["CONFLICT_UNRESOLVED"]
    assert "12000" in result.narrative
    assert result.recommended_action != "approve_candidate"


def test_third_proposed_call_becomes_revision_cap():
    async def propose_third(state):
        if int(state.get("eligibility_calls") or 0) >= 2:
            return "REVISE_ELIGIBILITY"
        return None

    document = scripted_document([_document()])
    eligibility = scripted_eligibility(
        [
            _eligibility(),
            _eligibility(decision="partially_eligible", payable=12000, attempt=2, disputes=[AMOUNT_DISPUTE]),
        ]
    )
    fraud = scripted_fraud(
        [_fraud(critical=True, risk="HIGH", disputes=[AMOUNT_DISPUTE], flags=["ARITHMETIC_MISMATCH"])]
    )
    result = _run(document, eligibility, fraud, proposer=propose_third)
    assert len(eligibility.calls) == 2
    assert result.reasons == ["REVISION_CAP"]
    assert result.recommended_action == "manual_review"
    rejected = [event for event in result.trace if event.kind == "plan_rejected"]
    assert rejected
    assert rejected[-1].payload["reason"] == "REVISION_CAP"


def test_approve_candidate_is_absent_when_fraud_is_critical():
    document = scripted_document([_document()])
    eligibility = scripted_eligibility([_eligibility(), _eligibility(attempt=2)])
    fraud = scripted_fraud(
        [
            _fraud(critical=True, risk="HIGH", flags=["PHASH_DUPLICATE"]),
            _fraud(critical=True, risk="HIGH", flags=["PHASH_DUPLICATE"], attempt=2),
        ]
    )
    result = _run(document, eligibility, fraud)
    assert len(fraud.calls) == 2
    assert fraud.calls[1].revision is not None
    assert fraud.calls[1].revision.question == "Cite the evidence for this flag in one sentence."
    assert fraud.calls[1].revision.flag == "PHASH_DUPLICATE"
    assert result.recommended_action != "approve_candidate"
    assert result.reasons == ["CONFLICT_UNRESOLVED"]


def test_clean_review_is_an_approve_candidate():
    document = scripted_document([_document()])
    eligibility = scripted_eligibility([_eligibility()])
    fraud = scripted_fraud([_fraud()])
    result = _run(document, eligibility, fraud)
    assert result.run_status == "ready"
    assert result.recommended_action == "approve_candidate"
    assert result.reasons == []
    assert len(document.calls) == 1
    assert len(eligibility.calls) == 1
    assert len(fraud.calls) == 1
    assert eligibility.calls[0].extracted_fields["bill_total"] == 18000
    assert eligibility.calls[0].ledger.remaining_balance == 500000


def test_ineligible_claim_is_a_reject_candidate():
    document = scripted_document([_document()])
    eligibility = scripted_eligibility([_eligibility(decision="ineligible", payable=0)])
    fraud = scripted_fraud([_fraud(risk="LOW")])
    result = _run(document, eligibility, fraud)
    assert result.recommended_action == "reject_candidate"
    assert result.reasons == ["INELIGIBLE"]


def test_low_groundedness_retries_eligibility_once():
    document = scripted_document([_document()])
    eligibility = scripted_eligibility(
        [
            _eligibility(decision="insufficient_evidence", groundedness=2, unsupported=["room rent is unlimited"]),
            _eligibility(attempt=2),
        ]
    )
    fraud = scripted_fraud([_fraud(risk="LOW")])
    result = _run(document, eligibility, fraud)
    assert len(eligibility.calls) == 2
    assert eligibility.calls[1].revision.unsupported_statements == ["room rent is unlimited"]
    assert eligibility.calls[1].revision.disputes == []
    assert result.recommended_action == "approve_candidate"


def test_changed_bill_total_sends_fraud_and_eligibility_again():
    document = scripted_document([_document(missing=["bill_total"]), _document(bill_total=12000, attempt=2)])
    eligibility = scripted_eligibility(
        [
            _eligibility(decision="insufficient_evidence", groundedness=2),
            _eligibility(payable=12000, attempt=2),
        ]
    )
    fraud = scripted_fraud([_fraud(risk="LOW"), _fraud(risk="LOW", attempt=2)])
    result = _run(document, eligibility, fraud)
    assert len(document.calls) == 2
    assert document.calls[1].revision.focus_fields == ["bill_total"]
    assert len(fraud.calls) == 2
    assert fraud.calls[1].revision.flag == "bill_total"
    assert len(eligibility.calls) == 2
    assert result.recommended_action == "approve_candidate"


def test_unchanged_fields_do_not_call_fraud_again():
    document = scripted_document([_document(missing=["bill_total"]), _document(attempt=2)])
    eligibility = scripted_eligibility(
        [
            _eligibility(decision="insufficient_evidence", groundedness=2),
            _eligibility(attempt=2),
        ]
    )
    fraud = scripted_fraud([_fraud(risk="LOW")])
    result = _run(document, eligibility, fraud)
    assert len(fraud.calls) == 1
    assert len(eligibility.calls) == 2
    assert result.recommended_action == "approve_candidate"


def test_a_running_review_is_not_started_twice():
    async def scenario():
        store = MemoryTraceStore()
        await store.start("c1", "run-existing")
        document = scripted_document([_document()])
        result = await run_review(
            ReviewRequest(claim_id="c1"),
            context=_context(),
            workers=_workers(document, scripted_eligibility([_eligibility()]), scripted_fraud([_fraud()])),
            store=store,
            use_model=False,
        )
        return result, document

    result, document = asyncio.run(scenario())
    assert result.review_run_id == "run-existing"
    assert result.run_status == "running"
    assert result.reasons == ["REVIEW_RUNNING"]
    assert document.calls == []


def test_missing_documents_escalate_without_workers():
    document = scripted_document([_document()])
    fraud = scripted_fraud([_fraud()])
    result = _run(document, scripted_eligibility([_eligibility()]), fraud, context=_context(documents=[]))
    assert result.reasons == ["NEEDS_REUPLOAD"]
    assert result.recommended_action == "manual_review"
    assert document.calls == []
    assert fraud.calls == []


def test_document_timeout_escalates_and_skips_fraud():
    async def slow(_request):
        await asyncio.sleep(5)

    eligibility = scripted_eligibility([_eligibility()])
    fraud = scripted_fraud([_fraud()])
    result = asyncio.run(
        run_review(
            ReviewRequest(claim_id="c1"),
            context=_context(),
            workers={"document": slow, "eligibility": eligibility, "fraud": fraud},
            use_model=False,
            document_timeout=0.05,
        )
    )
    assert result.run_status == "escalated"
    assert result.reasons == ["TIMEOUT"]
    assert fraud.calls == []
    assert result.document is not None
    assert result.document.error is not None
    assert result.document.error.code == "TIMEOUT"


def test_a_worker_failure_keeps_the_other_result_and_escalates():
    async def boom(_request):
        raise RuntimeError("fraud store unavailable")

    document = scripted_document([_document()])
    eligibility = scripted_eligibility([_eligibility()])
    result = asyncio.run(
        run_review(
            ReviewRequest(claim_id="c1"),
            context=_context(),
            workers={"document": document, "eligibility": eligibility, "fraud": boom},
            use_model=False,
        )
    )
    assert result.run_status == "escalated"
    assert result.reasons == ["TOOL_FAILED"]
    assert result.eligibility is not None
    assert result.eligibility.decision == "eligible"
    assert result.fraud is not None
    assert result.fraud.status == "error"
    assert result.recommended_action == "manual_review"


def test_policy_stops_before_a_third_worker_call():
    state = {
        "document_calls": 2,
        "eligibility_calls": 2,
        "fraud_calls": 1,
        "document": _document().model_dump(mode="json"),
        "eligibility": _eligibility(decision="partially_eligible", payable=12000).model_dump(mode="json"),
        "fraud": _fraud(critical=True, risk="HIGH", disputes=[AMOUNT_DISPUTE]).model_dump(mode="json"),
    }
    assert next_action(state) == "ESCALATE"
    assert recommended_action(state, escalated=True) == "manual_review"
