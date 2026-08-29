# Waggle Recover

Waggle Recover is a hackathon prototype for Razorpay's Revenue Recovery track: a bounded revenue-recovery agent that remembers prior outcomes, detects when that evidence has been superseded, and records exactly why every recovery action was selected or rejected. It supports payment failures and a second curated subscription/mandate failure path through the same pipeline.

It never processes money. Razorpay Test Mode webhooks are supported, but the product is fully demoable with its deterministic simulator and needs no external credentials.

The product thesis is simple: **remembering what worked is not enough—a recovery agent must know whether that memory is still authoritative.**

## Architecture and decision modes

Waggle Recover has two complementary primary modes:

- **Deterministic mode** is the default. It powers reproducible benchmarking and safe rule-based decisions without API keys, network access, Groq, or model calls.
- **AI Agent mode** runs a small LangGraph state machine. Waggle retrieves and temporally validates history first; a Groq-hosted Qwen model then proposes a candidate using trusted evidence only. The existing deterministic `PolicyEngine` remains the final authority.

The earlier simple `llm` provider remains available for compatibility, but it is not used by the benchmark.

```text
Payment or subscription revenue-risk event
      ↓
Normalization
      ↓
Waggle semantic retrieval
      ↓
Temporal / supersession validation
      ↓
Trusted evidence + explicitly rejected memory
      ↓
Recency-weighted Bayesian strategy priors
      ↓
LangGraph + Groq/Qwen (AI Agent mode only)
      ↓
Candidate recovery action
      ↓
Deterministic Merchant PolicyEngine
      ↓
Final bounded action / explicit human escalation
      ↓
Simulated execution / captured outcome
      ↓
Waggle memory
```

The LLM never directly executes payments and cannot override Waggle's stale/superseded status. Malformed output, timeouts, unknown evidence citations, invented methods, and model failures use the deterministic provider as a visible safe fallback.

Domain semantics live in tags and metadata on Waggle's existing nodes and edges. In particular, a replacement instrument creates `new --updates--> old`; historical evidence tied exclusively to the old instrument is retained for audit but vetoed from decision-making.

## Run it

The backend pins a tested Waggle Git revision, so it installs from a clean standalone checkout without requiring an adjacent repository.

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev,agent]"
uvicorn app.main:app --reload
```

Run the backend tests from the same activated environment:

```bash
python -m pytest -q
```

The dashboard is a small React/Vite client:

```bash
cd frontend
npm ci
npm run dev
```

Set `VITE_API_URL` only if the API is not at `http://localhost:8000`. Copy `backend/.env.example` to `backend/.env` to configure persistent database paths or Razorpay Test Mode. Do not commit the resulting `.env` file.

### Run with Docker

Docker Compose starts the API, persistent Waggle/SQLite storage, and the production-built dashboard without requiring local Python or Node.js installs:

```bash
docker compose up --build
```

Open `http://localhost:5173`; the API and health endpoint are available at `http://localhost:8000` and `http://localhost:8000/health`. The default container configuration uses deterministic decisions and Waggle's fake embedding model so the demo starts without credentials or model downloads. To use Qwen, provide runtime environment values—never bake them into an image:

```bash
GROQ_API_KEY='your runtime key' DECISION_PROVIDER=agent docker compose up --build
```

Stop the stack with `docker compose down`. Add `-v` only when you intentionally want to remove the named demo-data volume.

### AI Agent configuration

Set these only for a live Groq/Qwen demo:

```bash
export DECISION_PROVIDER=agent
export GROQ_API_KEY='your Groq key'
export GROQ_MODEL='qwen/qwen3.8-27b'
export AGENT_TEMPERATURE=0
export AGENT_TIMEOUT_SECONDS=15
```

`GROQ_MODEL` is always runtime-configurable; confirm the example against [Groq's current supported-model list](https://console.groq.com/docs/models). If credentials, the model, or LangGraph execution are unavailable, simulator AI mode reports the fallback rather than pretending an AI call succeeded.

## Demo and evaluation

Run a curated scenario:

```bash
curl -X POST 'http://localhost:8000/api/simulator/scenario/stale_card_trap/run?decision_mode=deterministic'
curl -X POST 'http://localhost:8000/api/simulator/scenario/stale_card_trap/run?decision_mode=agent'
curl -X POST 'http://localhost:8000/api/simulator/scenario/timing_memory/run?decision_mode=agent'
```

Other API entry points include `GET /api/simulator/scenarios/curated`, `POST /api/simulator/reset`, `GET /api/payments/`, `GET /api/payments/overview`, `POST /api/evaluation/run`, `GET /api/evaluation/reports`, `POST /api/mandate/recommend`, and `POST /api/mandate/scenarios/{scenario_id}/run`.

Curated product proofs now include Stale Card Trap, exact Timing Memory, Human Escalation, Policy Changed, and four subscription/mandate cases: valid timing memory, replaced instrument, exhausted-attempt escalation, and no authoritative memory.

The **Deterministic Policy Evaluation** compares three transparent systems on five curated/adversarial scenarios plus deterministic synthetic histories:

1. Blind fixed retry.
2. Contextual-history baseline (no update-chain traversal).
3. Waggle Recover (graph memory, supersession validation, policy, and audit trail).

Evaluation metrics are computed from simulator outcomes and persisted in SQLite; the dashboard never invents KPI values. The 200-case evaluation always constructs its own deterministic orchestrator, so `DECISION_PROVIDER=agent` never silently turns those benchmark numbers into Qwen results.

### Online strategy adaptation

Waggle also learns which **safe recovery strategy** works for each merchant and failure context. It computes recency-weighted Beta-Binomial estimates from authoritative `SUCCESS`/`FAILURE` recovery outcomes, using a fixed 14-day half-life, prior strength κ=5.0, and minimum effective sample size 5.0. Superseded or expired instrument outcomes contribute exactly zero. The estimates rank already-viable actions only; retry limits, permanent-failure rules, merchant constraints, and the deterministic `PolicyEngine` remain final authority.

The dashboard exposes these estimates as **Adaptive Strategy Memory**, including posterior success probability, effective sample size, and evidence count. The same compact audit is available to Qwen as trusted context in AI Agent mode.

A separate sealed sequential evaluator compares the original static deterministic policy with this adaptive ranking:

```bash
cd backend
python -m app.evaluation.sequential
```

Its protocol is fixed in source before results are observed: seeds `11, 29, 47, 71, 101`; three independent merchant streams; 30 cases per merchant; cold/intermediate/warm phases of 10 cases; identical pre-generated potential outcomes for both conditions; and merchant memory reset between streams. In every 10-case block, six controlled-exploration cases expose exactly one non-STOP action (`RETRY_AFTER`, `SUGGEST_METHOD`, `CUSTOMER_NUDGE`, repeated twice) and four decision-opportunity cases expose all three. The report therefore includes both overall optimal viable-action rate and the rate restricted to cases where multiple actions were actually available. This is intentionally separate from the existing 200-case isolated-scenario benchmark, whose reset semantics are unchanged.

After enforcing the intended precedence of exact authoritative timing memory over aggregate merchant priors, the preregistered run **did not support H1**. Static and adaptive results were identical across all five seeds: mean success **47.56%**, recovered GMV **₹230,700**, cumulative viable-action regret **₹49,971**, and overall optimal viable-action selection **73.33%**. On the 36 genuine decision opportunities per 90-case seed, both selected the optimal action **33.33%** of the time; the higher overall rate includes 54 forced-exploration cases where the sole viable action is automatically optimal. The same-customer, same-instrument, same-failure stream quickly produces exact retry-timing evidence, which correctly dominates generic strategy priors in both conditions. Earlier directional-uplift figures from the prior ordering are superseded and must not be published.

This evaluator isolates per-merchant sequential memory but does not test global-to-merchant hierarchical transfer: each merchant starts with a fresh graph, so its cold-start global prior is neutral rather than learned from other merchants. The prototype also bounds the newest outcomes per action only after taking a graph snapshot, and payment-ID correlation is not yet a general order/subscription recovery-episode identity.

The unchanged 200-case benchmark at seed 42 is summarized below:

| System | Parameter-aware action accuracy | Recovery success | Simulated GMV recovery | Exact stale-evidence rejection |
| --- | ---: | ---: | ---: | ---: |
| Waggle Recover | **100%** | **87%** | **87.3%** | **100%** |
| Contextual History | 76% | 76% | 77.3% | 0% |
| Blind Fixed Retry | 43% | 43% | 48.2% | 0% |

### Separate robustness evaluation

The robustness suite keeps the main 200-case demo intact and runs 1,000 isolated deterministic scenarios across fixed seeds `11, 29, 47, 83, 131`. It adds explicit blocked-method and temporal-policy-change cases to the existing coverage for transient, permanent, balance, route, replaced-instrument, conflicting-history, no-memory, alternative-method, and attempt-limit cases.

```bash
cd backend
WAGGLE_EMBEDDING_MODEL=fake python -c \
  'from app.evaluation.robustness import run_robustness_evaluation; run_robustness_evaluation(cache_path="data/evaluations/robustness.json")'
```

The verified System C result is **100% parameter-aware action accuracy**, **87% recovery success**, **87.38% simulated GMV recovery**, **100% stale rejection**, **0% unsafe-action rate**, **0% unnecessary-escalation rate**, and **0% policy-violation rate**. These are seeded simulator results, not production performance or production GMV.

### Temporal-authority ablation

The controlled 200-case ablation compares blind retry, contextual history, Waggle-style retrieval without temporal validation, and full Waggle Recover. Retrieval without temporal validation scored **76% action accuracy**, **76.92% simulated GMV recovery**, and used known stale evidence on **4.55% of stale-memory cases**. Full Waggle Recover scored **100% action accuracy**, **86.92% simulated GMV recovery**, **0% stale use**, and **100% stale rejection**. This is the direct evidence for the novelty claim: context helps, but retrieval alone is not enough; authority validation supplies the safety improvement.

```bash
cd backend
WAGGLE_EMBEDDING_MODEL=fake python -c \
  'from app.evaluation.ablations import run_ablation_evaluation; run_ablation_evaluation(cache_path="data/evaluations/ablations.json")'
```

### Separate Qwen evaluation

`app.evaluation.qwen` evaluates the Qwen candidate and the final post-policy action separately. It reports structured-output validity, candidate and final accuracy, rejected/stale citations, unknown evidence, policy modifications/blocks, safe escalation, fallback, latency, and token usage when available. Concise structured rows can be cached; prompts and chain-of-thought are never persisted.

This Qwen benchmark has **not been run for the checked-in result set**, because no runtime Groq credential was configured during verification. The dashboard therefore says “not run” instead of presenting deterministic fallbacks as Qwen results.

### Recovery episodes, escalation, and risk priority

Retry budgets are attached to a stable recovery episode, preferring subscription, mandate, invoice, order, then payment identity. Independent payments do not share attempts; repeated events in the same episode do. When attempts are exhausted, policy leaves no safe action, evidence materially conflicts, confidence falls below a review threshold, or merchant policy requires review, the system persists an `EscalationRecord` in SQLite and Waggle. The outcome is `SKIPPED`, human review is required, and money movement is `NONE`.

An explainable 0–100 risk score uses payment value, attempt count, failure class, active instruments, authoritative success history, and conflicts. It prioritizes the operations queue only and cannot bypass `PolicyEngine`.

## Safety boundaries

- Payment instrument aliases only: no PAN, CVV, banking credentials, or secrets.
- The allowed action set is closed: `RETRY_NOW`, `RETRY_AFTER`, `SUGGEST_METHOD`, `CUSTOMER_NUDGE`, `WAIT_NEXT_CYCLE`, `ESCALATE`, and `STOP`.
- A policy layer can allow, modify, or block a candidate decision.
- Qwen sees accepted evidence as usable memory; rejected memory is labeled forbidden and every cited evidence ID is validated deterministically.
- Agent traces contain structured summaries and latency only—never raw prompts, secrets, or hidden chain-of-thought.
- Razorpay webhook signatures are verified whenever Test Mode is enabled; webhook processing is idempotent per event/payment pair.
- Provider event IDs, duplicate protection, replay-window checks, clean malformed-payload responses, and test/simulation indicators are explicit. Real `payment.failed` events remain `PENDING`; only `payment.captured` can persist confirmed recovered money.
- Merchant policy versions form `new --updates--> old` chains. Superseded policies remain queryable for audit but are excluded from current authority.
- The subscription/mandate path is advisory only. It reuses recovery memory and policy but does not alter NPCI or bank retry schedules.

## Limitations

- All benchmark GMV and recovery outcomes are simulated.
- The deterministic 200-case and 1,000-case benchmarks do not evaluate Qwen.
- The separate Qwen evaluation runner exists and is tested with an injected model client, but no live Qwen benchmark result is claimed here.
- Real money movement is intentionally not automated in this prototype. A failure produces a recommendation and `PENDING`; confirmed recovery requires a capture webhook.
- Production performance, reliability, and recovery uplift have not been established.
- Adaptive merchant priors are advisory and did not demonstrate the preregistered sequential uplift after exact authoritative timing evidence was restored to its correct precedence.
- The subscription proof is four curated scenarios, not a complete recurring-payments product or rail integration.
- The risk score is deterministic queue prioritization, not a trained fraud or credit model.
