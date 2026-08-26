# Waggle Recover

Waggle Recover is a hackathon prototype for Razorpay's Revenue Recovery track: a bounded payment-recovery agent that remembers prior outcomes, detects when that evidence has been superseded, and records exactly why every recovery action was selected or rejected.

It never processes money. Razorpay Test Mode webhooks are supported, but the product is fully demoable with its deterministic simulator and needs no external credentials.

## The important loop

```text
normalized payment failure → Waggle retrieval → temporal / supersession validation
→ bounded decision → merchant-policy validation → simulated execution → outcome stored in Waggle
```

Domain semantics live in tags and metadata on Waggle's existing nodes and edges. In particular, a replacement instrument creates `new --updates--> old`; historical evidence tied exclusively to the old instrument is retained for audit but vetoed from decision-making.

## Run it

The backend is intended to be run beside a checkout of [Waggle-mcp](https://github.com/Abhigyan-Shekhar/Waggle-mcp), or with the published `waggle-mcp` package installed.

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload
```

For local development against the adjacent Waggle checkout:

```bash
PYTHONPATH=../../../src python -m pytest -q
```

The dashboard is a small React/Vite client:

```bash
cd frontend
npm install
npm run dev
```

Set `VITE_API_URL` only if the API is not at `http://localhost:8000`. Copy `backend/.env.example` to `backend/.env` to configure persistent database paths or Razorpay Test Mode. Do not commit the resulting `.env` file.

## Demo and evaluation

Run a curated scenario:

```bash
curl -X POST http://localhost:8000/api/simulator/scenario/stale-card-trap/run
```

Other API entry points include `GET /api/simulator/scenarios/curated`, `POST /api/simulator/reset`, `GET /api/payments/`, `GET /api/payments/overview`, `POST /api/evaluation/run`, and `POST /api/mandate/recommend`.

The evaluation harness compares three transparent systems on five curated/adversarial scenarios plus deterministic synthetic histories:

1. Blind fixed retry.
2. Contextual-history baseline (no update-chain traversal).
3. Waggle Recover (graph memory, supersession validation, policy, and audit trail).

Evaluation metrics are computed from simulator outcomes and persisted in SQLite; the dashboard never invents KPI values.

## Safety boundaries

- Payment instrument aliases only: no PAN, CVV, banking credentials, or secrets.
- The allowed action set is closed: `RETRY_NOW`, `RETRY_AFTER`, `SUGGEST_METHOD`, `CUSTOMER_NUDGE`, `WAIT_NEXT_CYCLE`, `ESCALATE`, and `STOP`.
- A policy layer can allow, modify, or block a candidate decision.
- Razorpay webhook signatures are verified whenever Test Mode is enabled; webhook processing is idempotent per event/payment pair.
- The mandate endpoint is advisory only. It does not alter NPCI or bank retry schedules.
