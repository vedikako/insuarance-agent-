# InsureFlow AI — full project reference

This document explains the whole project in plain language. It is written so you can explain it out loud, and so you can copy pieces into the synopsis PDF, the design PDF, and the short form fields.

The running name of the app is **InsureFlow AI**. The code lives in this repository. The public Git remote at the time this was written is `https://github.com/vedikako/insuarance-agent-.git`.

---

## 1. What this project is, in one picture

Imagine a person files a health or motor insurance claim. They upload a hospital bill, an ID, maybe a discharge summary or a repair estimate. An insurance company employee then has to answer three questions:

1. Are the papers readable and do they belong to this customer?
2. Does the policy actually cover this event, and for how much money?
3. Do the papers look inconsistent, duplicated, or otherwise suspicious?

Doing that by hand is slow, and a single chatbot that “just decides” is dangerous. A model can invent a clause, invent an amount, or approve a claim it should not touch.

InsureFlow splits the work across **specialist agents**. A **review orchestrator** is the manager. It does not read the bill itself. It calls workers in a fixed order, checks their answers against hard rules, and stops with a **recommendation**. A human at the insurance company still presses Accept or Reject. The agents never change the claim’s official status.

There is also a separate **policy assistant**. A customer (or the company, or an auditor) can ask questions about one policy they are allowed to see: what is covered, what is excluded, how much money is left. That chat does not approve claims.

There is also a separate **settlement calculator**. It applies deductible, copay, and the remaining balance with ordinary arithmetic. It does not use a language model.

---

## 2. Why we built it this way

A claim decision mixes three kinds of work that should not be one blob of text:

- **Reading documents** is messy. Files can be missing fields. Names can fail to match.
- **Coverage** must be tied to the actual policy wording and to the money still left on the policy. If the model says “pay 2 lakh” but the ledger only has 40,000 left, the ledger wins.
- **Fraud checks** should be repeatable. “The line items do not add up to the bill total” is a calculation, not a vibe.

So the design has four ideas:

1. **Specialists, not one giant prompt.** Each agent has one job and a typed input and output. The document agent cannot score fraud. The eligibility agent cannot read the raw file. The policy chat cannot approve a claim.
2. **The model proposes. Code decides.** Where a model is used, a later step clamps numbers, requires citations, or throws away an illegal next step. The orchestrator may ask Gemini for the next action, but a deterministic policy in `app/ai_agents/orchestrator/policy.py` is what actually runs. If the model suggests something illegal or a third call to the same worker, that suggestion is rejected and written into the trace as `plan_rejected`.
3. **A person owns the final yes or no.** `POST /api/reviews` returns a package. It does not set `claims.status`. Only `POST /api/claims/{claim_id}/decision`, made by an insurance-company user, writes `APPROVED` or `REJECTED`.
4. **Everything important is logged as a conversation between agents.** Each handoff is a row: who spoke, who they spoke to, what kind of message it was, and the payload. That log is the execution trace you submit.

---

## 3. Who uses it

There are three roles, stored on the user and carried in the login token.

| Role | What they can do |
| --- | --- |
| `CUSTOMER` | Sign in, see their own policies, file a claim with files, see their own claim status, chat about a policy they own. |
| `INSURANCE_COMPANY` | List claims, start the multi-agent review, run the simple fraud score, run settlement advice, accept or reject a claim. Company roles named `INSURER_ADMIN` and `CLAIM_ADJUSTER` are treated as this role in the API. |
| `AUDITOR` | Read policies (the access check allows auditors), use policy chat, and call the simple stored-claim fraud review. An auditor **cannot** start the multi-agent review and **cannot** accept or reject a claim. |

A customer only sees policies and claims tied to their own user id. The check is `can_read_policy` in `app/api/access.py`: company and auditor can read a policy; a customer can read it only when they are the owner.

---

## 4. The pages a person actually opens

The server is a FastAPI app named InsureFlow AI (`app/main.py`). These pages are plain HTML files, not a separate frontend framework.

| URL | Page | Who it is for |
| --- | --- | --- |
| `/` | Policy assistant (`chat.html`) | Ask questions about one selected policy. |
| `/file` | File a claim (`file.html`) | Customer uploads documents. |
| `/status` | Claim status (`status.html`) | Customer sees filed claims and whether they were accepted. |
| `/review` | Claim review (`review.html`) | Company user runs the agents and then Accept or Reject. The page says a person still approves or rejects. |

Login is `POST /api/auth/login` with email and password. On success the server sets an `access_token` cookie and also returns the token.

---

## 5. What happens in a real claim, start to finish

This is the story you should be able to tell without looking at code.

**Step A — the customer files.**

They pick a policy they own, type an amount and a short description, and upload one file per document type. Allowed types are hospital bill, discharge summary, Aadhaar, PAN, repair estimate, FIR, and RC. Allowed files are PDF, PNG, and JPEG. The amount must be positive and cannot be above the policy’s sum insured. The claim is stored as `SUBMITTED`. Files are saved under `data/uploads/{claim_id}/{document_id}/` with a cleaned filename so a weird filename cannot escape the folder.

**Step B — the company opens the review page.**

They choose the claim and call `POST /api/reviews` with `{ "claim_id": "..." }`. Only `INSURANCE_COMPANY` can do this.

**Step C — the orchestrator loads facts, not opinions.**

`load_claim_context` reads SQL: the claim, the policy dates and sum insured, how much of the base cover is already used, how many other claims this policy had in 30 and 90 days, the uploaded file rows, and the last four digits of an active Aadhaar or PAN on the customer. It does not send the full ID number into the review as the expected match. It sends `policyholder_id_last4`.

If there are no readable documents, the run stops immediately. Status is `escalated`, recommended action is `manual_review`, reason is `NEEDS_REUPLOAD`.

**Step D — documents first. Always.**

The first legal action is `CALL_DOCUMENT`. The document agent reads each file, extracts fields, checks identity, and labels the package:

- `accepted` — required files are present, required fields are filled, confidence is at least 0.60, and ID matches.
- `partial` — the required core is fine, but an optional document is weak. For health the optional file is the discharge summary. For motor it is the RC.
- `needs_reupload` — a required file or required field is missing, or confidence is too low.
- `kyc_failed` — an ID is present and the name or last four digits do not match the policyholder.

`kyc_failed` and `needs_reupload` escalate to a human. The eligibility and fraud workers are not called.

**Step E — eligibility and fraud run together.**

Only after the package is `accepted` or `partial` does the orchestrator send the same moment to two workers at once (`asyncio.gather`). They do not talk to each other directly. They both report back to the orchestrator.

**Step F — the manager may ask for one revision.**

Each worker may be called at most twice. The whole graph may take at most 8 steps. A second call is a critique, not a fresh unlimited chat. Examples:

- Fraud found a serious conflict (amount, date, identity, diagnosis). Eligibility is asked to answer those disputes.
- Eligibility’s groundedness score is under 4 and the document is missing fields. The document agent is asked to look again at those fields. If the watched fields `bill_total`, `holder_name`, or `admission_date` change, fraud is asked to re-check.
- Eligibility is positive but fraud risk is `HIGH` or `VERY_HIGH`. Fraud is asked to cite the evidence for its top flag in one sentence.
- If the problem is still there after the second try, the run escalates. Reasons include `CONFLICT_UNRESOLVED`, `REVISION_CAP`, `LOW_GROUNDEDNESS`, `KYC_FAILED`, `NEEDS_REUPLOAD`, `TIMEOUT`, `TOOL_FAILED`.

**Step G — a recommendation, then a human.**

The close step picks one of three labels using code, not the narrative model:

- `approve_candidate` — package ok, decision eligible or partially eligible, groundedness at least 4, no numeric inconsistency, fraud is not critical, risk is minimal/low/medium, and disputes are answered as sustain or revise.
- `reject_candidate` — eligibility says ineligible, the answer is grounded, and fraud is not critical.
- `manual_review` — everything else, including any escalation.

A short narrative is written for the human. If Gemini is available it may phrase the note, but the prompt says the recommended action is already chosen and must not be changed. If the model fails, a fixed template is used. The narrative cannot flip the action.

**Step H — the human decides.**

On the review page the company user accepts (optionally with a payable amount) or rejects. That writes `claims.status` to `APPROVED` or `REJECTED`, stores `approved_amount` and notes, and inserts a row in `claim_status_history`. The customer can then see that status on `/status`.

Settlement advice is a different button in the product sense: `POST /api/settlements`. It is not inside the review graph. It is a second opinion from arithmetic: deductible first, then copay, then cap at the remaining balance.

---

## 6. The agents, one by one

An “agent” here is a small LangGraph graph plus a public async function. Workers are not HTTP services. The orchestrator calls Python functions. The typed messages live in `app/ai_agents/contracts/messages.py`. That file is the contract: if you want to know what an agent is allowed to say, read the Pydantic models there.

### 6.1 Review orchestrator

**Job:** decide the next legal step, call workers, record the trace, stop with a recommendation.

**Graph:** `decide → (document | pair | eligibility | fraud | close) → decide again`, until finish or escalate.

**Files:** `app/ai_agents/orchestrator/`.

**Why a graph:** the same “look at state, pick one action” loop is easier to test than a long script full of nested ifs. The legal actions are a closed list:

`CALL_DOCUMENT`, `CALL_ELIGIBILITY_AND_FRAUD`, `REVISE_DOCUMENT`, `REVISE_ELIGIBILITY`, `REVISE_FRAUD`, `ESCALATE`, `FINISH`.

**How the model is kept on a leash:** `decide_node` may call a proposer. The proposer sees only a small status summary (call counts, package status, eligibility decision, groundedness, fraud critical flag, risk level). It must return JSON with one action. `resolve_proposal` then compares that action with `next_action(state)`. If they differ, or the proposal would be a third call, the proposal is rejected. The legal action runs instead. A rejected plan is traced.

**Timeouts:** document 60 seconds, eligibility 90, fraud 120. A timeout becomes a structured error with `needs_manual_review=True` and, for the orchestrator, an escalate reason of `TIMEOUT`.

**What it refuses to do:** it does not set claim status, and it does not call policy Q&A. That is written at the top of `runner.py` on purpose, so the chat agent cannot leak into claim decisions.

**One run at a time:** `review_runs` has a unique partial index so a claim cannot have two `running` reviews. A second request gets the in-progress package back.

### 6.2 Document agent

**Job:** read the uploaded files and say whether the package is usable. It does not score fraud and it does not decide coverage.

**Graph:** `plan → extract → validate → (reextract once) → extract → validate → finalize`.

**How extraction works:** `extract_document` first tries to parse the file bytes as JSON. That is how tests feed known fields. If the bytes are not JSON and a model is configured, it sends the document type, the focus fields, and up to 8,000 characters of text to Gemini with this rule: dates are `YYYY-MM-DD`, totals are numbers, use null when you cannot see a field, do not invent a name, date, or amount. Confidence is kept between 0 and 1. If required fields are missing and the model did not give a confidence, the score is 0.45. If they are all present, it is 0.95. A score under 0.60 is treated as weak.

**Required fields:**

- Hospital bill: patient name, hospital, admission date, discharge date, bill total, line items.
- Discharge summary: patient name, diagnosis.
- Aadhaar / PAN: holder name, id last 4.
- Repair estimate: customer name, workshop, estimate total.
- FIR: complainant name, incident date.
- RC: owner name, registration number.

**Required files:** a health claim needs a hospital bill and an Aadhaar or PAN. A motor claim needs a repair estimate or an FIR, plus an Aadhaar or PAN.

**KYC:** names are lowercased and extra spaces are collapsed, then compared. If the policy has a last-4 and the document has a last-4, those must match. If no ID was uploaded, this check does not invent a failure. If the ID is present but the name does not match, the package is `kyc_failed`.

**Internal retry:** on attempt 1, if a field is missing or confidence is low and KYC did not already fail, the graph re-reads once with those field names as the focus. The orchestrator can also call the whole agent a second time (`attempt=2`) with a `DocumentRevision`. Attempt 2 without a revision is a `BAD_INPUT` error.

### 6.3 Eligibility agent

**Job:** decide whether the quoted policy clauses and the money ledger support paying this claim, and for how much.

**Graph:** `retrieve → draft → judge → (one critique) → retrieve → draft → judge → finalize`.

Attempt 2 from the orchestrator skips the internal critique, because the orchestrator is already the one asking for the revision.

**Retrieve:** builds a search query from the diagnosis, the incident summary, the claim type, and words like hospitalisation, inpatient, cover, exclusion. It loads up to 4 chunks. Ranking is token overlap on `policy_chunks` for that policy’s product type (`app/services/policy_retrieval.py`). Quotes are cut at 400 characters. This is not a vector database yet. The comment in the retrieval module says embeddings can replace the ranker later without changing the return shape.

**Draft:** the safe default does not trust the model for the ceiling. `payable_limit` is the minimum of the claim amount, the remaining balance, and (when present) the sum of line items. The decision is then:

- payable ≤ 0 → `ineligible`
- payable is less than the claim amount → `partially_eligible`
- otherwise → `eligible`
- no clauses at all → `insufficient_evidence` and payable 0

If a model is configured, it may propose a decision and an amount. Finalize still clamps. If the proposed amount is above the limit, `numeric_inconsistency` is true, the reason `PAYABLE_ABOVE_LEDGER` is recorded, and the payable amount is pulled back down to the limit.

**Judge:** this is the groundedness check. Score 5 when a retrieved clause exists and the amount sits inside the ledger. Score 1 or 2 when the draft claims cover with no clause. A score under 4 on attempt 1 sends the agent back once with a broader query (day care, waiting period, room rent, sum insured). The orchestrator treats a score under 4 as “do not auto-approve.”

**Disputes:** when fraud sends conflicts, eligibility answers each one:

- `AMOUNT_CONFLICT` → stance `revise`, using the document’s number, then clamped to the ledger.
- `DATE_CONFLICT` or `IDENTITY_CONFLICT` → stance `revise`, payable 0.
- anything else, including diagnosis → `insufficient_evidence`.

If the revised amount equals the amount already proposed, the stance becomes `sustain` (the two sides actually agree).

**Currency** is fixed to `INR`.

**Citations** are part of the result. A cover decision with no citation is forced to `insufficient_evidence` and reason `NO_CITATION`.

### 6.4 Fraud — two different agents, do not mix them up

This is the easiest place to get confused in a demo, so say it clearly.

**A. Stored-claim rules scorer** — `run_fraud_review` in `app/ai_agents/fraud/runner.py`.

API: `POST /api/fraud/reviews`. Company or auditor. Graph is one node: load the claim from SQL, apply fixed rules, stop. **No model.** Flags include missing amount, claim above sum insured, claim above remaining balance, another claim with the same amount, inactive policy, approved amount above the claimed amount. Risk is `low`, `medium`, or `high`.

**B. Document forensics agent** — `run_fraud_agent`, used by the orchestrator.

This one reads the fields the document agent already extracted. Graph: `plan → act → act … → synthesize`, at most six tools.

The planner picks tools from what was uploaded:

| Tool | When it runs | What it looks for |
| --- | --- | --- |
| `arithmetic_gst` | Hospital bill or repair estimate | Line items vs bill total (difference over 1 rupee), bill vs claim amount, GST vs 18% of taxable amount. |
| `cross_document` | Any documents | Names vs policyholder, amounts that disagree, dates before policy start (`CLAIM_BEFORE_POLICY`) or outside the policy window. |
| `image_forensics` | PDF, PNG, or JPEG | SHA-256 of the file bytes compared with `known_hashes` on the document. A match is `PHASH_DUPLICATE`. If bytes cannot be read, it says no pixel check was run. It does not pretend it inspected pixels. |
| `id_redaction` | Aadhaar or PAN | A full identity number (more than 4 digits) sitting in a field other than `id_last4` is `REDACTION_SUSPECT`. |
| `behavioral` | 3 or more other claims in 30 days, or prior fraud flags | `BEHAVIORAL_VELOCITY`. |
| `content_review` | Discharge summary present | Empty diagnosis while another document names a condition is `CONTENT_CRITICAL`. |

Critical flags, which force the risk band up to at least `HIGH` and mark the result critical: `COPY_MOVE_DETECTED`, `PHASH_DUPLICATE`, `ARITHMETIC_MISMATCH`, `CLAIM_BEFORE_POLICY`, `CONTENT_CRITICAL`, `ID_MISMATCH`.

Score bands: under 0.20 minimal, under 0.40 low, under 0.60 medium, under 0.80 high, otherwise very high. The score kept is the **maximum** tool score, not an average. One serious tool is enough.

On attempt 2 the queue is a single tool chosen from the flag the orchestrator asked about, and the explanation must answer that question.

Fraud also emits **disputes** (`AMOUNT_CONFLICT`, `IDENTITY_CONFLICT`, `DATE_CONFLICT`, `DIAGNOSIS_CONFLICT`). Those are the structured arguments eligibility is asked to answer. This is how the two workers “disagree” without chatting in free text.

If every required tool fails, the result is `TOOL_FAILED`, not a fake “low risk.”

### 6.5 Policy Q&A agent

**Job:** answer questions about one already-selected policy. It does not approve or reject a claim. The synthesizer prompt says that in words.

**Graph:** `supervisor → (data → synthesizer) or stop`.

**Supervisor:** Gemini returns JSON: route `direct` or `data`. Direct is for greetings and “how do I use this chat.” It must not mention balances or coverage. If the route is unclear, the code forces `data`. That is the safe default: go look up facts instead of answering from memory.

**Data node:** only these tools exist, and at most 4 calls:

- `get_balance` — sum insured, amount used, amount left.
- `list_utilization_entries` — recent ledger rows.
- `get_policy_facts` — policy number, dates, premium, deductible.
- `retrieve_clauses` — up to 4 wording chunks. If the first search is empty, the question is rewritten once into a short search query (at most 12 words) and tried again.
- `list_claims_for_policy` — claim numbers, statuses, amounts.

Unknown tool names are rejected. If every call fails, the user hears that the records could not be read, not a made-up answer.

**Synthesizer rules that matter for security and honesty:**

- Copy rupee amounts exactly from the tool JSON. Do not round or invent.
- “How much is left” must use `remaining_balance`. “Already claimed” must use `utilized`.
- If clauses came back, quote one chunk verbatim and include its `chunk_id`.
- If no clauses came back, say the wording was not found. Do not describe a benefit that is not in the chunks.

API: `POST /api/policy-chat/sessions` then `POST /api/policy-chat/sessions/{id}/messages`. The session is checked so you cannot post into someone else’s chat. History sent to the model is the last 10 turns.

### 6.6 Settlement agent

**Job:** turn a claim amount plus policy terms into approve, partial, deny, or review.

**Graph:** one node, `assess`. **No model.** Timeout 30 seconds.

Order of math, and why the order matters:

1. If the amount is missing, deductible is negative, or copay is outside 0–100, stop at `review`. Do not guess.
2. If the policy is not active, or the amount is not positive, `deny` with payable 0.
3. Subtract the deductible first. If nothing remains, `deny` (`WITHIN_DEDUCTIBLE`).
4. Copay is the customer’s share of what remains. Payable = covered × (100 − copay) / 100.
5. If that payable is above the remaining balance, cap it. If the cap is still positive, the decision is `partial`. If the balance is zero, `deny`.

API: `POST /api/settlements`. Insurance company only.

---

## 7. How the agents communicate

They do **not** share a chat room. They do **not** call each other’s private nodes. Communication is three layers.

### Layer 1 — typed requests and results

The orchestrator builds a `DocumentAgentRequest`, `EligibilityAgentRequest`, or `FraudAgentRequest` and calls `run_document_agent`, `run_eligibility_agent`, or `run_fraud_agent`. The worker returns a result model. Extra fields the worker was not asked for are not how control flows.

Shared error shape:

```text
code: TIMEOUT | MODEL_UNAVAILABLE | TOOL_FAILED | BAD_INPUT
message: short text
retryable: true or false
```

### Layer 2 — the trace

Every step is appended to `agent_traces` (or an in-memory store in tests). Workers are not allowed to write this store. Only the orchestrator does. Columns: id, review_run_id, claim_id, seq, from_agent, to_agent, kind, payload JSON, created_at. Sequence is unique per run.

`from_agent` is orchestrator, document, eligibility, fraud, or system.
`to_agent` is orchestrator, document, eligibility, fraud, or human.

`kind` is one of:

| Kind | Meaning |
| --- | --- |
| `plan` | The manager chose the next action. |
| `plan_rejected` | The model suggested an action the policy refused. |
| `handoff` | First call to a worker, with the full request payload. |
| `critique` | Second call, telling the worker what to fix. |
| `result` | Worker finished a first attempt. |
| `revision` | Worker finished a second attempt. |
| `escalate` | Stop and hand the claim to a human, with a reason. |
| `error` | The worker timed out or threw. |

The finished package is stored on `review_runs.package`. Run status is `running`, `ready`, `escalated`, or `failed`.

A normal happy path looks like this. This is the shape of an execution trace:

```text
1  system → orchestrator   plan      action=CALL_DOCUMENT
2  orchestrator → document handoff   DocumentAgentRequest
3  document → orchestrator result    package_status=accepted
4  system → orchestrator   plan      action=CALL_ELIGIBILITY_AND_FRAUD
5  orchestrator → eligibility handoff
6  orchestrator → fraud    handoff
7  eligibility → orchestrator result decision=eligible, groundedness_score=5
8  fraud → orchestrator    result    risk_level=LOW, critical=false
9  system → orchestrator   plan      action=FINISH
```

Then the package’s `recommended_action` is `approve_candidate`. There is no escalate row, because nothing was handed to a human by the agents. The human still has to press Accept on the review page. That click is a different API (`/api/claims/{id}/decision`), not an agent trace row.

A conflict path adds critique and maybe escalate:

```text
plan CALL_DOCUMENT
handoff document → result accepted
plan CALL_ELIGIBILITY_AND_FRAUD
eligibility result eligible
fraud result critical=true, disputes=[AMOUNT_CONFLICT]
plan REVISE_ELIGIBILITY
critique eligibility → revision
plan ESCALATE
escalate orchestrator → human  reason=CONFLICT_UNRESOLVED
```

### Layer 3 — what is deliberately not communication

- Policy Q&A is not on this bus. A customer question cannot steer a claim review.
- Settlement is not on this bus. Its payable number is advice from rules, shown separately, so it cannot silently overwrite the eligibility payable.
- The narrative writer sees a snapshot and returns text. It has no tool that writes `recommended_action`.

---

## 8. AI security constraints we actually kept

This section is the one to use when someone asks “what did you do about AI risk?” Every item below is implemented in code, not only in a prompt.

### 8.1 The model is not the authority

- Next review action: model may propose; `resolve_proposal` accepts the proposal only when it equals the legal action. Otherwise the legal action runs and the rejection is logged.
- Payable amount: model may propose; `finalize_node` caps it at claim amount, remaining balance, and line-item total.
- Recommended action: chosen by `recommended_action()` from structured fields. The narrative prompt is forbidden from changing it, and the code never reads the narrative back into the action.
- Fraud forensics and settlement and the simple fraud scorer do not ask a model to invent a risk label. The label comes from thresholds and flags.
- Tests refuse to call Gemini unless a test explicitly opts in (`PYTEST_CURRENT_TEST` blocks the key).

### 8.2 Least privilege for tools

Policy Q&A has an allowlist of five tool names and a cap of four calls. A model cannot invent `delete_claim` and have it run. Fraud forensics has an allowlist of six tools and a cap of six. Document extraction has one tool, `extract_document`.

### 8.3 Grounding

Eligibility will not treat a claim as covered without a retrieved clause. Missing citations force `insufficient_evidence`. Low groundedness blocks `approve_candidate`. Policy answers must quote a chunk or admit the wording was not found. Amounts in chat must be copied from tool output.

### 8.4 Bounded autonomy

- At most two calls per worker.
- At most eight orchestrator steps.
- One internal re-extract inside the document agent.
- One internal critique inside eligibility, and not on orchestrator attempt 2.
- Timeouts on every worker call.
- One running review per claim.
- If the loop cannot settle a conflict, it escalates. It does not keep talking.

### 8.5 Human authority and separation of duties

- Agents return `approve_candidate` or `reject_candidate`. Those words are recommendations. The database status changes only when an insurance-company user posts `accept` or `reject`.
- Customers cannot start reviews, settlements, or decisions.
- Auditors can inspect the simple fraud score and can chat about policies. They cannot decide a claim and cannot start the multi-agent review.
- The review runner does not call the policy assistant.

### 8.6 Identity, secrets, and data minimization

- Passwords are checked with bcrypt. The password is SHA-256 hex-digested first so very long passwords still fit bcrypt’s input limit. The hash in the database is not the raw password.
- Access tokens are JWT (`HS256`) with `sub`, `role`, `type=access`, and a 15-minute expiry. The signing key comes from `SECRET_KEY` in the environment. The default `dev-only-change-me` is a development placeholder and must be replaced before any real deployment.
- The cookie is `HttpOnly` (JavaScript on the page cannot read it), `SameSite=lax`, and `Secure` is a setting (`COOKIE_SECURE`) that should be true behind HTTPS.
- Every authenticated request reloads the user from the database and rejects inactive users. A token alone is not enough if the account was turned off.
- Login failures are a single “Invalid credentials” message. The API does not say whether the email exists.
- KYC comparison uses the name and the last four digits. The redaction tool flags documents that still contain a longer digit string.
- SQL uses bound parameters (`:claim_id`), not string concatenation, so a claim id cannot become a SQL injection.
- Uploaded names are stripped to a safe character set and stored as the file’s base name only.
- Uploads are limited to the document-type allowlist and the PDF/PNG/JPEG allowlist.
- The Gemini API key is `GCP_API_KEY` from the environment, not committed in code. Temperature is 0.1 so answers are less random. Retries are capped at 2. Timeout on the model client is 60 seconds.

### 8.7 Failure is visible

A timed-out or crashed worker becomes `status=error` and `needs_manual_review=true`. The orchestrator escalates. It does not fill in a cheerful “low risk” or “eligible.” If claim context cannot be loaded, the package is `failed` with `BAD_INPUT`. If workers cannot even be constructed, the package is `failed` with `TOOL_FAILED`.

### 8.8 Prompt-level limits (these help, they are not the whole defense)

- Document prompt: do not invent a name, date, or amount.
- Eligibility prompts: do not exceed the remaining balance, do not invent a clause, score 5 only when amounts are in the clauses or the ledger.
- Policy supervisor: direct replies must not mention balances or coverage. If unsure, use the data route.
- Policy synthesizer: do not approve or reject a claim; do not describe benefits that are not in the retrieved chunks.
- Narrative prompt: do not change the recommended action and do not tell the reviewer to ignore it.

The code checks after the prompt. That is the part that still holds if the model ignores the instructions.

### 8.9 What we did not claim

Be honest about this in a viva:

- Image forensics here is a hash comparison against hashes supplied on the document, not a full copy-move or perceptual-hash lab. `COPY_MOVE_DETECTED` is a critical flag the policy knows about; the current image tool emits `PHASH_DUPLICATE` when the SHA-256 matches.
- Clause search is keyword overlap, not embeddings.
- Chat sessions are in memory on the server process, not a durable chat database.
- The document model path sends extracted text, up to 8,000 characters. It is not a full scanned-image OCR pipeline by itself. JSON fixtures are the reliable path in tests.
- This is an advisory system. It is not an autonomous payer.

---

## 9. API map

All JSON routes are under `/api`.

| Method and path | Who | What you send | What you get |
| --- | --- | --- | --- |
| `POST /api/auth/login` | Anyone with an account | email, password | token, user id, role, name, and an HttpOnly cookie |
| `GET /api/policies` | Signed-in user | — | Policies. Customers only see their own. |
| `POST /api/claims` | Customer | multipart: policy_id, claim_amount, description, incident_date, document_types, files | 201 and the new claim plus document ids. Status `SUBMITTED`. |
| `GET /api/claims` | Signed-in user | — | Claim list. Customers only see their own. |
| `POST /api/reviews` | Insurance company | `{ "claim_id" }` | `ReviewPackage`: run status, recommended action, reasons, document result, eligibility result, fraud result, narrative, trace |
| `POST /api/claims/{claim_id}/decision` | Insurance company | `{ "choice": "accept" or "reject", "payable_amount": optional }` | Updated claim. Accept writes `APPROVED` and the payable amount. Reject writes `REJECTED`. |
| `POST /api/fraud/reviews` | Company or auditor | `{ "claim_id" }` | Simple fraud result: risk low/medium/high, flags, summary |
| `POST /api/settlements` | Insurance company | `{ "claim_id" }` | decision approve/partial/deny/review, payable amount, notes, summary |
| `POST /api/policy-chat/sessions` | Customer, company, or auditor who may read that policy | `{ "policy_id" }` | session_id |
| `POST /api/policy-chat/sessions/{id}/messages` | Same user who owns the session | `{ "message" }` | reply, route, citations, balance if a tool returned one, tool call records |

Database URL and the Gemini key come from `.env`: `DATABASE_URL`, `GCP_API_KEY`, `SECRET_KEY`, `ALGORITHM`, token lifetimes, cookie flags. The app uses async SQLAlchemy and PostgreSQL (`asyncpg`). Alembic migration `20261011_01` creates `agent_traces` and `review_runs`. The rest of the insurance tables (users, policies, claims, claim_documents, policy_chunks, kyc_documents, policy_utilization_entries, claim_status_history) are the existing schema this service reads and writes.

---

## 10. How a recommendation is calculated (the rules in order)

First matching rule wins inside `_decide`:

1. If the document agent has not run, call it.
2. If it errored, escalate with `TIMEOUT` or `TOOL_FAILED`.
3. If the package is `kyc_failed`, escalate `KYC_FAILED`.
4. If the package is not `accepted` or `partial`, escalate `NEEDS_REUPLOAD`.
5. If a second document pass just finished, eligibility must be revised. If watched money/name/date fields changed and fraud has a call left, both are called again.
6. If eligibility and fraud have not run, call both.
7. If either errored, escalate.
8. If fraud is critical and has disputes and eligibility has run once, revise eligibility.
9. If that was already the second eligibility call, escalate `CONFLICT_UNRESOLVED`.
10. If groundedness is under 4 and fields are missing and the document agent has run once, revise the document agent.
11. If that was already the second document call, escalate `REVISION_CAP`.
12. If groundedness is under 4, nothing is missing, and eligibility has run once, revise eligibility for unsupported statements.
13. If that was already the second eligibility call, finish with reason `LOW_GROUNDEDNESS` (the recommendation will still be manual review, because groundedness is under 4).
14. If eligibility is positive and fraud risk is high or very high, and fraud has run once, revise fraud.
15. If fraud was already revised and the risk is still high, escalate `CONFLICT_UNRESOLVED`.
16. Otherwise finish.

Then `recommended_action` maps that ending state to approve candidate, reject candidate, or manual review, using the checks in section 5, step G.

---

## 11. Technology, and why each piece is there

| Piece | Why |
| --- | --- |
| FastAPI | HTTP API and the four HTML pages, with dependency injection for the current user and the database session. |
| Pydantic | Every agent message is validated. A worker cannot return a random shape. |
| LangGraph | Each agent is an explicit graph of nodes. You can point at the picture and say what runs next. |
| Gemini through `langchain-google-genai` | The only language model. Used for document extraction when the file is not already JSON, eligibility drafting, the optional action proposal, the advisory narrative, and policy chat. Model name in code: `gemini-3.8-flash`. |
| PostgreSQL + SQLAlchemy async + Alembic | Durable claims, policies, ledger, traces. |
| python-jose + bcrypt | Login tokens and password checks. |
| pytest + httpx | API and agent tests, including fakes so tests do not need a live model. |

---

## 12. Words to use when you explain it

Use these, and you will sound precise:

- **Recommendation, not decision.** The agents recommend. The company user decides.
- **Orchestrator.** The manager graph.
- **Workers.** Document, eligibility, and fraud forensics.
- **Contract.** The Pydantic request and result for each worker.
- **Trace.** The ordered log of plans, handoffs, results, critiques, and escalations.
- **Groundedness.** A 1–5 score for whether the coverage answer is supported by retrieved clauses.
- **Revision cap.** Two tries per worker, then a human.
- **Critical flag.** A fraud finding that blocks automatic approval.
- **Ledger.** Sum insured, amount already paid, amount still left. The payable amount cannot exceed what is left.

Avoid saying “the AI approves the claim.” The page, the API, and the database update are a person.
