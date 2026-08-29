"""Fail-closed webhook verification and replay-safe event identity."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class SignatureVerification:
    valid: bool
    mode: str
    reason: str = ""


class RazorpaySignatureVerifier:
    def verify(self, *, body: bytes, signature: str, secret: str, enabled: bool) -> SignatureVerification:
        if not enabled:
            return SignatureVerification(valid=True, mode="simulation", reason="Razorpay Test Mode disabled")
        if not secret or not signature:
            return SignatureVerification(valid=False, mode="razorpay_test", reason="Webhook verification is not configured")
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return SignatureVerification(
            valid=hmac.compare_digest(expected, signature),
            mode="razorpay_test",
            reason="" if hmac.compare_digest(expected, signature) else "Invalid webhook signature",
        )


def provider_event_identity(*, explicit_event_id: str, body: bytes) -> str:
    return explicit_event_id.strip() or f"body_{hashlib.sha256(body).hexdigest()}"


def event_is_replay(*, event_created_at: datetime, now: datetime, replay_window_seconds: int) -> bool:
    if event_created_at.tzinfo is None:
        event_created_at = event_created_at.replace(tzinfo=UTC)
    age = (now - event_created_at).total_seconds()
    return age > replay_window_seconds or age < -300
