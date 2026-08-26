"""Bounded adviser for recurring-payment recovery.

It recommends customer-facing steps around externally operated bank/NPCI attempts;
it never controls or reschedules the underlying rail.
"""
from __future__ import annotations

from app.domain.enums import RecoveryAction
from app.domain.models import MandateContext


def recommend_mandate_recovery(context: MandateContext) -> dict[str, object]:
    """Return one permitted action plus an auditable explanation."""
    allowed = set(context.allowed_actions)
    if context.previous_failures >= 3 and RecoveryAction.STOP in allowed:
        action, reason = RecoveryAction.STOP, "Configured stop rule reached after repeated failed cycles."
    elif context.previous_failures >= 2 and RecoveryAction.ESCALATE in allowed:
        action, reason = RecoveryAction.ESCALATE, "Repeated cycle failures warrant assisted recovery."
    elif context.instrument_id and context.previous_failures >= 1 and RecoveryAction.SUGGEST_METHOD in allowed:
        action, reason = RecoveryAction.SUGGEST_METHOD, "Ask the customer to update the payment method before the next external attempt."
    elif context.previous_failures >= 1 and RecoveryAction.CUSTOMER_NUDGE in allowed:
        action, reason = RecoveryAction.CUSTOMER_NUDGE, "Send a payment-method reminder; no bank retry is initiated."
    else:
        action, reason = RecoveryAction.WAIT_NEXT_CYCLE, "Wait for the externally scheduled mandate attempt."

    return {
        "mandate_id": context.mandate_id,
        "action": action.value,
        "reason": reason,
        "rail_control": "none",
        "next_scheduled_at": context.next_scheduled_at.isoformat() if context.next_scheduled_at else None,
    }
