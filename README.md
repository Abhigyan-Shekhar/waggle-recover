# Waggle Recover

Waggle Recover is a hackathon prototype for Razorpay's Revenue Recovery track: a bounded payment-recovery agent that remembers prior outcomes, detects when that evidence has been superseded, and records exactly why every recovery action was selected or rejected.

It never processes money. Razorpay Test Mode webhooks are supported, but the product is fully demoable with its deterministic simulator and needs no external credentials.

The product thesis is simple: **remembering what worked is not enough—a recovery agent must know whether that memory is still authoritative.**

## Architecture and decision modes

Waggle Recover has two complementary primary modes:

- **Deterministic mode** is the default. It powers reproducible benchmarking and safe rule-based decisions without API keys, network access, Groq, or model calls.
- **AI Agent mode** runs a small LangGraph state machine. Waggle retrieves and temporally validates history first; a Groq-hosted Qwen model then proposes a candidate using trusted evidence only. The existing deterministic `PolicyEngine` remains the final authority.

The earlier simple `llm` provider remains available for compatibility, but it is not used by the benchmark.

```text
Razorpay event
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
Final bounded action
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

Other API entry points include `GET /api/simulator/scenarios/curated`, `POST /api/simulator/reset`, `GET /api/payments/`, `GET /api/payments/overview`, `POST /api/evaluation/run`, and `POST /api/mandate/recommend`.

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

The unchanged 200-case benchmark at seed 42 produced **100% parameter-aware action accuracy**, **87% recovery success**, **87.3% simulated GMV recovery**, and **100% exact stale-evidence rejection** for Waggle Recover, compared with 76%/76%/77.3%/0% for contextual history and 43%/43%/48.2%/0% for blind retry.

## Safety boundaries

- Payment instrument aliases only: no PAN, CVV, banking credentials, or secrets.
- The allowed action set is closed: `RETRY_NOW`, `RETRY_AFTER`, `SUGGEST_METHOD`, `CUSTOMER_NUDGE`, `WAIT_NEXT_CYCLE`, `ESCALATE`, and `STOP`.
- A policy layer can allow, modify, or block a candidate decision.
- Qwen sees accepted evidence as usable memory; rejected memory is labeled forbidden and every cited evidence ID is validated deterministically.
- Agent traces contain structured summaries and latency only—never raw prompts, secrets, or hidden chain-of-thought.
- Razorpay webhook signatures are verified whenever Test Mode is enabled; webhook processing is idempotent per event/payment pair.
- The mandate endpoint is advisory only. It does not alter NPCI or bank retry schedules.
