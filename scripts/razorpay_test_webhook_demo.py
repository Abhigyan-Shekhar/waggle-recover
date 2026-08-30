#!/usr/bin/env python3
"""Send a signed Razorpay Test Mode failure followed by capture confirmation."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid


def signed_post(url: str, secret: str, event_id: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Webhook rejected ({exc.code}): {detail}") from exc


def payment_payload(
    event: str,
    payment_id: str,
    event_id: str,
    recovery_execution_id: str | None = None,
) -> dict:
    failed = event == "payment.failed"
    entity = {
        "id": payment_id,
        "order_id": f"order_{payment_id.removeprefix('pay_')}",
        "amount": 800000,
        "currency": "INR",
        "status": "failed" if failed else "captured",
        "method": "card",
        "card": {"last4": "9988"},
        "merchant_id": "MERCH-RAZORPAY-DEMO",
        "notes": {
            "customer_id": "CUST-RAZORPAY-DEMO",
            **({"recovery_execution_id": recovery_execution_id} if recovery_execution_id else {}),
        },
        "created_at": int(time.time()),
    }
    if failed:
        entity.update({
            "error_code": "issuer_unavailable",
            "error_description": "Issuer temporarily unavailable",
            "error_source": "issuer",
            "error_step": "payment_authorization",
            "error_reason": "issuer_unavailable",
        })
    return {"id": event_id, "event": event, "payload": {"payment": {"entity": entity}}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a signed Razorpay Test Mode recovery through Waggle Recover.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/webhooks/razorpay")
    parser.add_argument("--hold-seconds", type=float, default=2.0, help="Pause before the capture webhook.")
    args = parser.parse_args()

    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit("Set RAZORPAY_WEBHOOK_SECRET in this shell and in the backend environment.")

    suffix = uuid.uuid4().hex[:10]
    payment_id = f"pay_demo_{suffix}"
    failed_id = f"evt_demo_failed_{suffix}"
    captured_id = f"evt_demo_captured_{suffix}"

    print("1/2 Sending signed payment.failed webhook…")
    failed = signed_post(args.url, secret, failed_id, payment_payload("payment.failed", payment_id, failed_id))
    decision = failed.get("decision", {})
    outcome = failed.get("outcome", {})
    outcome_status = outcome.get("outcome", "PENDING") if isinstance(outcome, dict) else outcome
    print(f"    verified={failed.get('mode') == 'razorpay_test'} action={decision.get('action', '—')} outcome={outcome_status}")
    execution_id = (failed.get("execution") or {}).get("id")

    time.sleep(max(0, args.hold_seconds))
    print("2/2 Sending signed payment.captured webhook…")
    captured_payment_id = f"pay_captured_{suffix}" if execution_id else payment_id
    captured = signed_post(
        args.url,
        secret,
        captured_id,
        payment_payload("payment.captured", captured_payment_id, captured_id, execution_id),
    )
    print(f"    status={captured.get('status')} updated_attempts={captured.get('updated_attempts', 0)} recovered=INR 8,000")
    print(f"Demo complete: {payment_id} now has a signed webhook audit trail and captured recovery outcome.")


if __name__ == "__main__":
    main()
