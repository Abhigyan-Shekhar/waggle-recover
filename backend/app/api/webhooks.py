"""Razorpay webhook endpoint with signature verification and idempotency."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.main import get_db, get_orchestrator
from app.persistence.database import Database
from app.razorpay.normalizer import normalize_razorpay_event, verify_webhook_signature
from app.recovery.orchestrator import RecoveryOrchestrator

router = APIRouter()


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
    db: Database = Depends(get_db),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    body = await request.body()
    payload = json.loads(body)

    # 1. Signature verification: local demo mode may be unsigned, enabled
    # Razorpay mode fails closed on missing credentials or signature.
    signature_valid = False
    if not settings.razorpay_enabled:
        signature_valid = True
    elif settings.razorpay_webhook_secret and x_razorpay_signature:
        signature_valid = verify_webhook_signature(
            body=body,
            signature=x_razorpay_signature,
            webhook_secret=settings.razorpay_webhook_secret,
        )
        if not signature_valid:
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    else:
        raise HTTPException(status_code=503, detail="Razorpay webhook verification is not configured")

    # 2. Idempotency — deduplicate by event + payment_id
    event_type = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = entity.get("id", str(uuid.uuid4()))
    event_id = f"{event_type}:{payment_id}"
    event_hash = hashlib.sha256(event_id.encode()).hexdigest()[:16]

    webhook_record = {
        "id": event_hash,
        "event_type": event_type,
        "payment_id": payment_id,
        "raw_payload": json.dumps(payload),
        "signature_valid": 1 if signature_valid else 0,
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

    result = orchestrator.process_event(event=event, simulate=False)
    db.mark_webhook_processed(event_hash)
    return {**result, "webhook_id": event_hash}
