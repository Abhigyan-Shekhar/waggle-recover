"""Optional signed n8n handoff for already-final human escalations."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.domain.models import EscalationRecord, PaymentFailure, RecoveryDecision


class N8nEscalationHandoff:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client

    def configured(self) -> bool:
        return bool(
            self.settings.n8n_enabled
            and self.settings.n8n_escalation_webhook_url.startswith(("https://", "http://localhost", "http://127.0.0.1"))
            and self.settings.n8n_webhook_secret
        )

    def send(
        self,
        record: EscalationRecord,
        failure: PaymentFailure,
        decision: RecoveryDecision,
    ) -> dict[str, Any]:
        if not self.configured():
            return {"status": "DISABLED", "provider": None, "workflow_id": None}
        payload = {
            "event_type": "recovery.human_review_required",
            "escalation_id": record.id,
            "recovery_episode_id": record.recovery_episode_id,
            "merchant_id": record.merchant_id,
            "customer_id": record.customer_id,
            "amount": record.amount,
            "currency": failure.currency,
            "failure_code": failure.failure_code,
            "candidate_action": record.candidate_action.value,
            "final_action": "ESCALATE",
            "attempt_count": record.attempts_used,
            "max_attempts": record.max_automated_attempts,
            "risk_score": decision.risk_score,
            "risk_band": decision.risk_band,
            "reason": record.escalation_reason,
            "accepted_evidence_ids": record.accepted_evidence_ids,
            "rejected_evidence_count": len(record.rejected_evidence_ids),
            "recommended_manual_next_step": record.recommended_manual_next_step,
            "created_at": record.created_at.isoformat(),
            "money_movement": "NONE",
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self.settings.n8n_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        client = self.client or httpx.Client(timeout=self.settings.n8n_timeout_seconds)
        close = self.client is None
        try:
            response = client.post(
                self.settings.n8n_escalation_webhook_url,
                content=body,
                headers={"content-type": "application/json", "x-waggle-signature": signature},
            )
            response.raise_for_status()
            data = response.json() if response.content else {}
            workflow_id = str(data.get("workflow_id") or data.get("executionId") or data.get("id") or record.id)
            return {
                "status": "CREATED",
                "provider": "n8n",
                "workflow_id": workflow_id[:200],
                "created_at": datetime.now(UTC),
            }
        except (httpx.HTTPError, ValueError):
            return {
                "status": "FAILED",
                "provider": "n8n",
                "workflow_id": None,
                "created_at": datetime.now(UTC),
            }
        finally:
            if close:
                client.close()
