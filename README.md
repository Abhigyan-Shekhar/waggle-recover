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

## Safety boundaries

- Payment instrument aliases only: no PAN, CVV, banking credentials, or secrets.
- The allowed action set is closed: `RETRY_NOW`, `RETRY_AFTER`, `SUGGEST_METHOD`, `CUSTOMER_NUDGE`, `WAIT_NEXT_CYCLE`, `ESCALATE`, and `STOP`.
- A policy layer can allow, modify, or block a candidate decision.
- Qwen sees accepted evidence as usable memory; rejected memory is labeled forbidden and every cited evidence ID is validated deterministically.
- Agent traces contain structured summaries and latency only—never raw prompts, secrets, or hidden chain-of-thought.
- Razorpay webhook signatures are verified whenever Test Mode is enabled; webhook processing is idempotent per event/payment pair.
- The mandate endpoint is advisory only. It does not alter NPCI or bank retry schedules.
