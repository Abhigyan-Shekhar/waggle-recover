"""Deterministic recovery-episode identity and correlation."""
from __future__ import annotations

import hashlib

from app.domain.models import NormalizedPaymentEvent, RecoveryEpisode


def recovery_episode_for(event: NormalizedPaymentEvent) -> RecoveryEpisode:
    scopes = (
        ("subscription", event.subscription_id),
        ("mandate", event.mandate_id),
        ("invoice", event.invoice_id),
        ("order", event.order_id),
        ("payment", event.payment_id),
    )
    scope_type, scope_id = next(((kind, value) for kind, value in scopes if value), ("payment", event.payment_id))
    identity = "|".join((event.merchant_id, event.customer_id, scope_type, scope_id))
    episode_id = f"rep_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    return RecoveryEpisode(
        id=episode_id,
        scope_type=scope_type,
        scope_id=scope_id,
        external_payment_id=event.payment_id,
        order_id=event.order_id,
        subscription_id=event.subscription_id,
        mandate_id=event.mandate_id,
        invoice_id=event.invoice_id,
        customer_id=event.customer_id,
        merchant_id=event.merchant_id,
    )
