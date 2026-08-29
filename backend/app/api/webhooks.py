"""Razorpay webhook endpoint with signature verification and idempotency."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.main import get_db, get_orchestrator
from app.persistence.database import Database
from app.razorpay.normalizer import normalize_razorpay_event
from app.razorpay.webhook_security import RazorpaySignatureVerifier, event_is_replay, provider_event_identity
from app.recovery.orchestrator import RecoveryOrchestrator

router = APIRouter()
LOGGER = logging.getLogger(__name__)
SIGNATURE_VERIFIER = RazorpaySignatureVerifier()


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
    x_razorpay_event_id: str = Header(None),
    db: Database = Depends(get_db),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    body = await request.body()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")

    # 1. Signature verification: local demo mode may be unsigned, enabled
    # Razorpay mode fails closed on missing credentials or signature.
    verification = SIGNATURE_VERIFIER.verify(
        body=body,
        signature=x_razorpay_signature if isinstance(x_razorpay_signature, str) else "",
        secret=settings.razorpay_webhook_secret,
        enabled=settings.razorpay_enabled,
    )
    if not verification.valid:
        status_code = 503 if "configured" in verification.reason else 400
        raise HTTPException(status_code=status_code, detail=verification.reason)

    # 2. Idempotency — deduplicate by event + payment_id
    event_type = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = str(entity.get("id", ""))
    if not event_type or not payment_id:
        raise HTTPException(status_code=422, detail="Webhook requires event type and payment entity id")
    provider_event_id = provider_event_identity(
        explicit_event_id=(x_razorpay_event_id if isinstance(x_razorpay_event_id, str) else "") or str(payload.get("id", "")),
        body=body,
    )
    event_hash = provider_event_id.removeprefix("body_")[:64]

    webhook_record = {
        "id": event_hash,
        "provider_event_id": provider_event_id,
        "event_type": event_type,
        "payment_id": payment_id,
        "raw_payload": json.dumps(payload),
        "signature_valid": 1 if verification.valid else 0,
        "processed": 0,
        "created_at": datetime.now(UTC).isoformat(),
    }

    is_new = db.upsert_webhook_event(webhook_record)
    if not is_new:
        return {"status": "duplicate", "event_id": event_hash}

    # 3. Normalize and process
    event = normalize_razorpay_event(payload)
    if not event:
        return {"status": "skipped", "reason": f"Unsupported event: {event_type}"}

    event.event_id = provider_event_id
    event.test_mode = verification.mode != "live"
    if settings.razorpay_enabled and event_is_replay(
        event_created_at=event.created_at,
        now=datetime.now(UTC),
        replay_window_seconds=settings.webhook_replay_window_seconds,
    ):
        raise HTTPException(status_code=409, detail="Webhook event is outside the replay window")

    LOGGER.info("Webhook accepted type=%s payment=%s event=%s mode=%s", event_type, payment_id, event_hash[:12], verification.mode)
    result = orchestrator.process_event(event=event, simulate=False)
    db.mark_webhook_processed(event_hash)
    return {
        **result,
        "webhook_id": event_hash,
        "event_id": provider_event_id,
        "mode": verification.mode,
        "test_mode": verification.mode != "live",
        "recovery_simulated": False,
    }
