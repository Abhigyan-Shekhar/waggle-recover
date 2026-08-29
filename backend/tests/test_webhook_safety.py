"""Webhook identity, duplicate, and failure-semantics tests."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

import app.main  # noqa: F401
from app.api.webhooks import razorpay_webhook
from app.config import Settings
from app.persistence.database import Database
from app.razorpay.webhook_security import RazorpaySignatureVerifier, event_is_replay


class RequestStub:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


class OrchestratorSpy:
    def __init__(self) -> None:
        self.calls = 0

    def process_event(self, **_kwargs):
        self.calls += 1
        return {"status": "processed"}


def payload(event: str = "payment.failed") -> bytes:
    return json.dumps({
        "id": "evt_provider_001",
        "event": event,
        "payload": {"payment": {"entity": {
            "id": "pay_webhook_001",
            "order_id": "order_webhook_001",
            "amount": 250000,
            "currency": "INR",
            "method": "card",
            "card": {"last4": "1234"},
            "notes": {"customer_id": "CUST-WEBHOOK"},
            "merchant_id": "MERCH-WEBHOOK",
            "error_code": "issuer_unavailable",
            "error_description": "Issuer unavailable",
            "created_at": int(datetime.now(UTC).timestamp()),
        }}},
    }).encode()


def invoke(body: bytes, db: Database, orchestrator: OrchestratorSpy):
    return asyncio.run(razorpay_webhook(
        request=RequestStub(body),
        x_razorpay_signature=None,
        x_razorpay_event_id="evt_provider_001",
        db=db,
        orchestrator=orchestrator,
        settings=Settings(razorpay_enabled=False),
    ))


def test_duplicate_provider_event_is_processed_once(tmp_path):
    db = Database(tmp_path / "webhooks.db")
    orchestrator = OrchestratorSpy()

    first = invoke(payload(), db, orchestrator)
    duplicate = invoke(payload(), db, orchestrator)

    assert first["status"] == "processed"
    assert first["event_id"] == "evt_provider_001"
    assert first["mode"] == "simulation"
    assert duplicate["status"] == "duplicate"
    assert orchestrator.calls == 1


def test_malformed_webhook_has_clean_400(tmp_path):
    with pytest.raises(HTTPException) as exc:
        invoke(b"{not-json", Database(tmp_path / "bad.db"), OrchestratorSpy())
    assert exc.value.status_code == 400
    assert exc.value.detail == "Malformed webhook JSON"


def test_enabled_webhook_verification_fails_closed_without_secret():
    result = RazorpaySignatureVerifier().verify(
        body=b"{}", signature="", secret="", enabled=True
    )
    assert result.valid is False
    assert result.mode == "razorpay_test"


def test_replay_window_rejects_old_or_implausibly_future_events():
    now = datetime.now(UTC)
    assert event_is_replay(
        event_created_at=now - timedelta(days=2), now=now, replay_window_seconds=86400
    )
    assert event_is_replay(
        event_created_at=now + timedelta(minutes=6), now=now, replay_window_seconds=86400
    )
    assert not event_is_replay(
        event_created_at=now - timedelta(minutes=2), now=now, replay_window_seconds=86400
    )
