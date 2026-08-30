# Waggle Recover

> Remembering what worked is not enough. A recovery agent must know whether that memory is still authoritative.

Waggle Recover is a deployment-believable Razorpay Revenue Recovery submission. It turns a payment failure into a bounded recommendation, optionally opens a Razorpay **Test Mode** Payment Link, waits for provider confirmation, and preserves the complete evidence and policy trail in Waggle and SQLite. It never performs an automatic card charge and never treats a prediction, a failed-payment event, or a Payment Link creation as recovered revenue.

All benchmark money in this repository is **SIMULATED GMV**. No production uplift or production reliability is claimed.

## WHAT WAGGLE RECOVER IS

Waggle Recover is an auditable recovery control plane for payment and subscription revenue risk. Razorpay supplies signed payment events and confirms outcomes; Waggle establishes which historical facts are current; a deterministic provider or Qwen proposes a bounded action; the deterministic `PolicyEngine` remains final authority; and an optional n8n workflow hands terminal escalations to a human.

The closed action set is `RETRY_NOW`, `RETRY_AFTER`, `SUGGEST_METHOD`, `CUSTOMER_NUDGE`, `WAIT_NEXT_CYCLE`, `ESCALATE`, and `STOP`. `STOP` and `ESCALATE` are absorbing episode states and always mean no money movement.

The dashboard includes curated payment/subscription scenarios, a live temporal-authority shadow, Razorpay Test Mode execution state, a 20–50 case batch queue, an immutable merchant-policy editor, optional signed n8n handoff, decision graphs, and frozen evaluation reports.

## WHY CONTEXTUAL HISTORY IS NOT ENOUGH

Semantic relevance can retrieve a successful retry for a card that has since been replaced, a route the merchant has blocked, or a policy version that is no longer in force. A context-only system can confidently reuse exactly the wrong fact.

Waggle Recover separates retrieval from authority. It retrieves broadly for audit, then permits only evidence proven `CURRENT` into trusted decision context. `UNKNOWN`, stale, superseded, expired, and conflicting evidence fails closed. Rejected evidence remains visible to operators, but Qwen receives only its count and rejection categories—never rejected IDs, labels, methods, instruments, timing, outcomes, or reasons.

## ARCHITECTURE

```text
Razorpay payment event / normalized revenue-risk event
  → normalization
  → stable RecoveryEpisode identity
  → Waggle semantic retrieval
  → temporal and supersession authority validation
  → trusted evidence only
  → deterministic or Qwen/LangGraph candidate
  → deterministic Merchant PolicyEngine
  → bounded action / terminal STOP / terminal ESCALATE
  → simulator or Razorpay Test Mode execution provider
  → provider-confirmed payment.captured outcome
  → Waggle + SQLite audit graph

ESCALATE → EscalationRecord → optional signed n8n webhook
         → human review only; no money movement
```

The execution-provider boundary has simulation and Razorpay Test Mode implementations. The batch runner invokes the normal `RecoveryOrchestrator` once per independent case. Merchant policy is not duplicated in application tables; Waggle's versioned policy graph is the authority.

## TEMPORAL AUTHORITY

Instrument replacement creates `new --updates--> old`. The old node and its outcomes remain queryable, while `valid_to` and update-chain traversal prevent them from influencing a current decision. Policy versions use the same temporal rule. Evidence without enough identity or time information is `UNKNOWN` and rejected rather than optimistically trusted.

`GET /api/evaluation/authority-shadow/curated_003` runs one scenario in two isolated temporary stores. Both sides have the same scenario, graph, retrieval, ranking, provider, policy, retry budget, and simulated outcomes; only temporal validation changes. Neither shadow result is persisted as a real recovery attempt. The UI obtains checked-in ablation values from the report API rather than hard-coded marketing copy.

## RECOVERY EPISODES

Retry budgets and terminal states belong to a stable `RecoveryEpisode`, preferring subscription, mandate, invoice, order, then payment identity. Independent payments do not consume one another's attempt budget. Duplicate delivery of the same failed event reuses the same episode and execution. A capture can confirm only the execution explicitly linked through recovery notes, Payment Link identity, or provider lookup for that payment.

`STOP` and `ESCALATE` are irreversible for an episode. A webhook replay, policy update, model result, or failed external handoff cannot restart automation.

## QWEN + POLICY BOUNDARY

Deterministic mode is the default and powers deterministic evaluations without network or model calls. Agent mode runs a small LangGraph workflow using a runtime-configured Groq-hosted Qwen model. Qwen proposes from sanitized, accepted evidence; it does not execute payments and cannot override temporal status.

Malformed output, timeout, invented method, unknown citation, or provider failure produces a visible deterministic fallback. The `PolicyEngine` always validates the candidate last. This boundary matters because the frozen Qwen evaluation reached 52% candidate accuracy and 60% post-policy accuracy—not 100%—even though all 50 responses were structurally valid.

## RAZORPAY TEST MODE EXECUTION

The dashboard includes an in-app **Razorpay Test Lab**. It composes a signed
`payment.failed` test event, passes it through the same Waggle retrieval,
temporal-authority, policy, execution, and audit pipeline as the webhook API,
then shows the resulting Payment Link and webhook stream in the UI.

Without credentials, the lab uses a clearly labeled local Razorpay-compatible
mock checkout. Its `payment.captured` event confirms that mock execution but is
never included in provider-confirmed Razorpay GMV. With the Test Mode
configuration below, eligible recoveries create a real Standard Payment Link
through Razorpay's Test Mode API; finish those payments on Razorpay's hosted
mock Checkout and let the verified webhook confirm recovery. No real money is
charged in either mode. Razorpay currently limits Standard Payment Links in
Test Mode to 30 per business.

The optional provider uses Razorpay's official Standard Payment Link API, `POST /v1/payment_links`. It sends amount, currency, `accept_partial=false`, an expiry, unique reference, description, non-sensitive recovery identifiers in notes, and notification settings. No PAN, CVV, raw payment credentials, customer phone/email, API key, or webhook secret is stored in the execution or returned by the API.

Enable it only with Test Mode credentials:

```bash
RAZORPAY_ENABLED=true
RAZORPAY_TEST_EXECUTION_ENABLED=true
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...
RAZORPAY_MOCK_LAB_ENABLED=true
```

Live-key IDs are rejected. Eligible, policy-approved `SUGGEST_METHOD` and `CUSTOMER_NUDGE` decisions may create one idempotent Payment Link per episode/execution type. Creation persists `PENDING`, returns a safe public URL, and records zero recovered money. Missing configuration leaves the simulator functional.

A verified `payment.captured` webhook is authoritative. The handler verifies HMAC, enforces a replay window, deduplicates provider events, resolves the exact execution/episode, checks amount and currency, and only then records `SUCCESS` and recovered amount. An invalid signature, unrelated payment, mismatch, provider error, or merely created link leaves recovery unconfirmed.

To demo the full loop, open **Test Lab**, send a preset failed-payment event,
inspect the pending recovery operation, and complete the local mock checkout.
Try the failure button first to verify the link stays pending, then success to
see the signed capture, exact execution correlation, and audit graph. When Test
Mode credentials are connected, the operation instead opens Razorpay Checkout.

See [the Test Mode walkthrough](docs/RAZORPAY_TEST_WEBHOOK_DEMO.md) and Razorpay's official [Payment Link create API](https://razorpay.com/docs/api/payments/payment-links/create-standard/), [fetch API](https://razorpay.com/docs/api/payments/payment-links/fetch-all-standard/), and [payment webhook documentation](https://razorpay.com/docs/webhooks/payments/).

## BATCH RECOVERY

`POST /api/batches/demo?count=25` creates one merchant batch of 20–50 isolated normalized failures. Every case uses the normal retrieval, authority validation, provider, policy, executor, and audit persistence. Dashboard rows open the existing decision graph. Operators can sort by risk/value and filter by action, human review, or stale-evidence rejection.

The aggregate deliberately separates:

- **SIMULATED RECOVERY** — deterministic scenario outcome, never production revenue;
- **TEST MODE PENDING** — a created link, never counted as recovered;
- **PROVIDER-CONFIRMED RECOVERY** — only a linked capture webhook;
- safely stopped and human-review GMV — no money movement.

It also reports action counts, policy blocks, stale memories rejected, unsafe actions, and policy violations.

## HUMAN ESCALATION / N8N

`ESCALATE` first creates the internal SQLite and Waggle `EscalationRecord`. If n8n is configured, Waggle Recover then sends a minimal HMAC-SHA256-signed payload containing identifiers, amount/currency, failure/action/risk summaries, evidence IDs/count, reason, and the manual next step. It sends no instrument credentials or provider secrets.

```bash
N8N_ENABLED=true
N8N_ESCALATION_WEBHOOK_URL=https://your-n8n.example/webhook/waggle-recover-human-review
N8N_WEBHOOK_SECRET=...
```

The returned workflow ID/status is stored on the escalation. Disabled n8n preserves the internal queue. Failure leaves the decision escalated and cannot resume automation. STOP and normal decisions emit no handoff; episode replay creates no duplicate. Import [the sample internal-review workflow](n8n/waggle-recover-human-review.json), configure its `N8N_WEBHOOK_SECRET`, and enable Code-node access to Node's `crypto` module. It verifies the signature and terminal boundary before returning a demo workflow ID, without another paid destination.

## MERCHANT POLICY VERSIONING

The policy console edits the existing `MerchantPolicy`: attempt limit, retry bounds, allowed actions, blocked methods/routes, confidence threshold, and human-review requirements. Saving creates an immutable Waggle node, adds `NEW_POLICY --updates--> OLD_POLICY`, invalidates the old node for future decisions, and retains the timeline for audit. The newest current version is chosen deterministically.

Current policy is loaded before decision validation. Historical successes remain visible even when current policy blocks their method, but cannot bypass current rules. Invalid retry bounds/confidence, duplicate actions, and out-of-range attempts are rejected. Policy mutation can be protected with `PROTECT_MUTATION_ENDPOINTS=true` and `MUTATION_API_TOKEN`.

## RUN LOCALLY

The backend pins its Waggle dependency and runs without external credentials:

```bash
cd backend
uv sync --all-extras --frozen
WAGGLE_EMBEDDING_MODEL=fake uv run uvicorn app.main:app --reload
```

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Or use `docker compose up --build`; data persists in `waggle-data`. Runtime secrets belong in an uncommitted `.env`, never in images/source. Agent mode additionally needs `DECISION_PROVIDER=agent`, `GROQ_API_KEY`, and a currently supported runtime `GROQ_MODEL`.

## EVALUATIONS

All deterministic money below is **SIMULATED GMV**. Scenario labels, weights, potential outcomes, retry budgets, and frozen Qwen rows are not adjusted to improve metrics.

| Evaluation | Current checked result |
| --- | --- |
| Deterministic 200-case, seed 42 | Waggle: **100%** parameter-aware action accuracy, **87%** success, **86.92% SIMULATED GMV**, **100%** stale rejection. Contextual History: 76%, 76%, 76.92%, 0%. Blind Fixed Retry: 43%, 43%, 48.04%, 0%. |
| Five-seed 1,000-case robustness | **100%** parameter-aware accuracy, **87%** success, **87.38% SIMULATED GMV**, **100%** stale rejection, **0** unsafe actions, **0** unnecessary escalations, **0** policy violations. |
| Controlled 200-case authority ablation | Validation OFF: **89%** accuracy, **76.92% SIMULATED GMV**, **4.55%** known-stale usage. Validation ON: **100%** accuracy, **86.92% SIMULATED GMV**, **0%** stale use, **100%** stale rejection. |
| Frozen Qwen, seed 31415, 50 live calls | **100%** structurally valid output, **52%** candidate accuracy, **60%** post-policy accuracy, 0 fallback, 0 hallucinated citations, 0 stale citations, 0 rejected-memory use; mean latency 5,964.36 ms. |

Deterministic and Qwen reports are intentionally separate. Cached Qwen rows contain no prompts, context, secrets, or hidden reasoning. The Qwen cache is not rerun in normal CI.

```bash
cd backend
.venv/bin/ruff check app tests
WAGGLE_EMBEDDING_MODEL=fake .venv/bin/pytest -q

cd ../frontend
npm ci
npm run build
```

## LIMITATIONS

- All benchmark recovery and GMV are simulated; no production revenue uplift is established.
- Razorpay execution is Test Mode Payment Links only. There is no automatic card charging or unrestricted retry.
- Provider-confirmed recovery requires a correlated, verified `payment.captured`; an unpaid link remains pending.
- Deterministic benchmarks do not evaluate Qwen. The frozen 50-call run is too small to establish production model quality.
- Production performance, reliability, webhook delivery behavior, and recovery uplift have not been established.
- Subscription recovery is a bounded proof, not a complete recurring-payments or bank-rail integration.
- Risk scoring prioritizes a queue; it is not a trained fraud, credit, or collections model.
- Merchant strategy priors are advisory. The preregistered sequential experiment showed no uplift after exact current timing evidence regained correct precedence; no adaptive-strategy uplift should be claimed.
- The sample n8n workflow is a demo handoff, not a production case-management system.

## 5-MINUTE DEMO

1. **00:00–00:25 — Thesis.** Relevant history is not necessarily authoritative history.
2. **00:25–01:10 — Stale Card Trap.** Run `curated_003`; show old success, replacement edge, rejected audit evidence, sanitized Qwen trace, and the current-policy action.
3. **01:10–01:40 — Why Waggle.** Run the shadow: validation OFF uses stale timing; ON removes it. Show cached ablation metrics.
4. **01:40–02:20 — Razorpay Test Mode.** Signed `payment.failed` → approved Payment Link → `PENDING` and recovered zero → complete Test Mode payment → `payment.captured` → provider-confirmed recovery.
5. **02:20–02:50 — Policy Changed.** Save a version blocking card; show immutable history and a current decision where current policy wins.
6. **02:50–03:20 — Escalation.** Exhaust attempts; show terminal `ESCALATE`, money movement `NONE`, and n8n ID/disabled/failed state. Replay cannot restart it.
7. **03:20–04:00 — Batch.** Run 25 cases; show separate money classes, zero unsafe actions/violations, filters, and one decision graph.
8. **04:00–04:35 — Deterministic proof.** Show 200-case, 1,000-case, and authority-ablation reports; always say SIMULATED GMV.
9. **04:35–04:55 — Qwen boundary.** Show 100% structured output but 52% candidate/60% post-policy accuracy and zero rejected-memory use: the model is not final financial authority.
10. **04:55–05:00 — Close.** “Waggle decides what memory is authoritative. Qwen proposes. Policy decides what is allowed. Razorpay confirms whether money was actually recovered.”

Safe claim: **100% parameter-aware action accuracy on the current seeded deterministic 200-case benchmark**, not production. Safe claim: **temporal validation improved deterministic accuracy from 89% to 100% in the controlled seeded ablation**. Never call Test Mode capture real customer revenue, a created Payment Link recovered money, Qwen 100% accurate, or adaptive strategies uplifted.
