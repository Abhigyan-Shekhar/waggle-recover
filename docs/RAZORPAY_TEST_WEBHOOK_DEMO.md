# Razorpay Test Mode webhook demo

This demo sends two realistic, HMAC-signed webhook requests through the public webhook endpoint. With the explicit execution flags and `rzp_test_*` credentials below, the first event can create an actual Razorpay Test Mode Payment Link:

```text
payment.failed → Waggle evidence retrieval → PolicyEngine-approved decision
→ Razorpay Test Mode Payment Link → PENDING / recovered ₹0
payment.captured → exact execution correlation → SQLite + Waggle → provider-confirmed recovery
```

It never charges a card automatically and never moves real money. Payment Link creation is not recovery; the verified capture webhook is the only source of truth for success.

## Run

Use the same private test value in the backend and sender shell:

```bash
cd backend
RAZORPAY_ENABLED=true \
RAZORPAY_TEST_EXECUTION_ENABLED=true \
RAZORPAY_KEY_ID='rzp_test_...' \
RAZORPAY_KEY_SECRET='your-test-secret' \
RAZORPAY_WEBHOOK_SECRET='local-test-secret' \
WAGGLE_EMBEDDING_MODEL=fake \
uv run uvicorn app.main:app --reload
```

In another terminal:

```bash
RAZORPAY_WEBHOOK_SECRET='local-test-secret' \
python scripts/razorpay_test_webhook_demo.py
```

Keep the dashboard open at `http://127.0.0.1:5173`. The decision inspector shows the safe Payment Link URL and `WAITING FOR payment.captured`. The included script uses a synthetic capture for local contract testing; for the complete provider demo, complete the Test Mode Payment Link and allow Razorpay to deliver the real signed `payment.captured` webhook.

For a Razorpay Dashboard delivery, configure the webhook URL as `/api/webhooks/razorpay` and use the same Test Mode webhook secret. The endpoint verifies the signature, rejects old replays, deduplicates provider event IDs, and never treats `payment.failed` as recovered money.
