# InsureFlow AI

InsureFlow helps an insurance company review a health or motor claim. The customer uploads the papers. Specialist agents read those papers, check the policy wording and the money still left, and look for inconsistent or suspicious amounts. They return a recommendation. A person at the insurance company still accepts or rejects the claim. The agents do not change the claim status.

A separate policy assistant answers questions about one policy: what is covered, and how much is left. That chat does not approve claims.

## How a claim moves

1. The customer signs in, picks one of their policies, and files a claim with the amount, a short description, and the documents.
2. A company user opens the claim and starts a review.
3. The review orchestrator loads the claim, the policy, the remaining balance, and the uploaded files from the database.
4. The document agent runs first. It extracts fields and checks that the identity document matches the policyholder.
5. If the package is usable, eligibility and fraud run together. They do not read each other's answers. Both report back to the orchestrator.
6. The orchestrator may ask one worker to look again. Each worker can be called at most twice.
7. The run ends with `approve_candidate`, `reject_candidate`, or `manual_review`, plus a short note and a trace of every handoff.
8. The company user accepts or rejects. That is the only step that writes `APPROVED` or `REJECTED`.

If the identity check fails, or a required file is missing, eligibility and fraud are not called. The review stops for a person.

## The agents

| Agent | What it does |
| --- | --- |
| Review orchestrator | Chooses the next legal step, calls the workers, and writes the trace. A model may suggest a step. `policy.py` keeps only a legal one. |
| Document | Reads each file and labels the package `accepted`, `partial`, `needs_reupload`, or `kyc_failed`. |
| Eligibility | Decides whether the policy clauses and the remaining balance support a payment, and for how much. Amounts are capped by the ledger. |
| Fraud forensics | Checks the extracted fields: bill math, names, dates, file hash, identity redaction, and claim velocity. |
| Policy Q&A | Answers a question about one selected policy from the ledger and the policy wording. |
| Settlement | Applies deductible, then copay, then the remaining balance. No model. |
| Stored-claim fraud score | A separate rules check on a claim already in the database. It is not the forensics agent used inside the review. |

Money figures come from PostgreSQL. A model is not allowed to invent a balance or a clause that was not retrieved.

## Pages

| URL | Who uses it |
| --- | --- |
| `/` | Policy assistant |
| `/file` | Customer files a claim |
| `/status` | Customer sees claim status |
| `/review` | Company user runs the review, then accepts or rejects |

Sign in with `POST /api/auth/login`. Roles are `CUSTOMER`, `INSURANCE_COMPANY`, and `AUDITOR`. Customers only see their own policies and claims. An auditor can read and chat, and can run the stored-claim fraud score. An auditor cannot start the multi-agent review or decide a claim.

## Run it locally

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set `DATABASE_URL`, `SECRET_KEY`, and `GCP_API_KEY`. Then:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

Tests:

```bash
pytest
```

A longer explanation of each agent, the review rules, and the API is in `docs/PROJECT_REFERENCE.md`.
