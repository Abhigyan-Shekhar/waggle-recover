"""Normalize multiple revenue-risk types into the shared recovery pipeline."""
from __future__ import annotations

from app.domain.models import NormalizedPaymentEvent, RevenueRiskEvent

SUPPORTED_RISK_TYPES = {"PAYMENT_FAILURE", "SUBSCRIPTION_FAILURE"}


def normalize_revenue_risk(event: RevenueRiskEvent) -> NormalizedPaymentEvent:
    risk_type = event.risk_type.upper().strip()
    if risk_type not in SUPPORTED_RISK_TYPES:
        raise ValueError(f"Unsupported revenue risk type: {event.risk_type}")
    payment_id = event.payment_id or f"risk_{event.event_id}"
    return NormalizedPaymentEvent(
        event_type="payment.failed",
        payment_id=payment_id,
        customer_id=event.customer_id,
        merchant_id=event.merchant_id,
        amount=event.amount,
        currency=event.currency,
        method=event.method,
        instrument_id=event.instrument_id,
        error_code=event.failure_code,
        error_description=event.failure_reason,
        created_at=event.created_at,
        source="subscription" if risk_type == "SUBSCRIPTION_FAILURE" else "revenue_risk",
        event_id=event.event_id,
        test_mode=event.test_mode,
        subscription_id=event.subscription_id,
        mandate_id=event.mandate_id,
        raw_payload={
            "risk_type": risk_type,
            "next_billing_at": event.next_billing_at.isoformat() if event.next_billing_at else None,
        },
    )
