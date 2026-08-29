"""Razorpay event normalizer — transforms webhook payloads to NormalizedPaymentEvent."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.models import NormalizedPaymentEvent


def normalize_razorpay_event(payload: dict[str, Any]) -> NormalizedPaymentEvent | None:
    """
    Normalize a Razorpay webhook payload to NormalizedPaymentEvent.
    Returns None for unsupported event types.
    """
    event_type = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    if not entity:
        return None

    supported = {"payment.failed", "payment.captured", "payment.authorized"}
    if event_type not in supported:
        return None

    # Extract timestamp
    created_at_ts = entity.get("created_at", 0)
    try:
        created_at = datetime.fromtimestamp(created_at_ts, tz=UTC)
    except Exception:
        created_at = datetime.now(UTC)

    # Extract error info (only populated for payment.failed)
    error = entity.get("error_code", "") or ""
    error_desc = entity.get("error_description", "") or ""
    error_source = entity.get("error_source", "") or ""
    error_step = entity.get("error_step", "") or ""
    error_reason = entity.get("error_reason", "") or ""

    # Customer ID: Razorpay uses contact + email as identifier, or notes
    notes = entity.get("notes", {}) or {}
    customer_id = (
        notes.get("customer_id")
        or notes.get("user_id")
        or entity.get("contact", "")
        or entity.get("email", "unknown")
    )

    # Instrument identifier (safe, anonymized)
    instrument_id = _extract_instrument_id(entity)

    return NormalizedPaymentEvent(
        event_type=event_type,
        payment_id=entity.get("id", ""),
        order_id=entity.get("order_id", ""),
        customer_id=str(customer_id),
        merchant_id=entity.get("merchant_id", "razorpay_demo"),
        amount=int(entity.get("amount", 0)),
        currency=entity.get("currency", "INR"),
        method=entity.get("method", "unknown"),
        instrument_id=instrument_id,
        route=entity.get("internal_error_code", ""),
        error_code=error,
        error_description=error_desc,
        error_source=error_source,
        error_step=error_step,
        error_reason=error_reason,
        created_at=created_at,
        raw_payload=payload,
        source="razorpay",
        event_id=str(payload.get("id", "")),
        test_mode=True,
        subscription_id=str(notes.get("subscription_id", "")),
        mandate_id=str(notes.get("mandate_id", "")),
        invoice_id=str(notes.get("invoice_id", "")),
    )


def _extract_instrument_id(entity: dict[str, Any]) -> str:
    """Extract a safe, anonymized instrument identifier."""
    method = entity.get("method", "")

    if method == "card":
        card = entity.get("card", {}) or {}
        last4 = card.get("last4", "")
        if last4:
            return f"card_{last4}"
        token = entity.get("token_id", "")
        if token:
            return f"card_{token[-6:]}"

    elif method == "upi":
        vpa = entity.get("vpa", "")
        if vpa:
            # Hash VPA for anonymity
            import hashlib
            hashed = hashlib.sha256(vpa.encode()).hexdigest()[:8]
            return f"upi_{hashed}"

    elif method == "netbanking":
        bank = entity.get("bank", "unknown")
        return f"nb_{bank.lower()}"

    elif method == "wallet":
        wallet = entity.get("wallet", "unknown")
        return f"wallet_{wallet.lower()}"

    return ""


def verify_webhook_signature(
    body: bytes,
    signature: str,
    webhook_secret: str,
) -> bool:
    """Verify Razorpay webhook signature using HMAC-SHA256."""
    import hashlib
    import hmac

    expected = hmac.new(
        key=webhook_secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
