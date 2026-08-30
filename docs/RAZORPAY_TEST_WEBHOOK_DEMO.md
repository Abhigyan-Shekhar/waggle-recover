# Razorpay Test Mode webhook demo

This demo sends two realistic, HMAC-signed webhook requests through the public webhook endpoint:

```text
payment.failed → Waggle evidence retrieval → bounded decision → PENDING
payment.captured → captured outcome written to SQLite + Waggle → recovered GMV confirmed
```

It does not call a payment API or move money. The second webhook is the source of truth for recovery success.

## Run

Use the same private test value in the backend and sender shell:

```bash
cd backend
RAZORPAY_ENABLED=true \
RAZORPAY_WEBHOOK_SECRET='local-test-secret' \
WAGGLE_EMBEDDING_MODEL=fake \
uv run uvicorn app.main:app --reload
```

In another terminal:

```bash
RAZORPAY_WEBHOOK_SECRET='local-test-secret' \
python scripts/razorpay_test_webhook_demo.py
```

Keep the dashboard open at `http://127.0.0.1:5173`, then refresh it after the script completes. The activity feed and decision graph will show the webhook-originated recovery and its captured outcome.

For a Razorpay Dashboard delivery, configure the webhook URL as `/api/webhooks/razorpay` and use the same Test Mode webhook secret. The endpoint verifies the signature, rejects old replays, deduplicates provider event IDs, and never treats `payment.failed` as recovered money.
