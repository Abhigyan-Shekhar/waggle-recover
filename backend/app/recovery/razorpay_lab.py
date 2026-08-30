"""Razorpay Test Lab orchestration for the in-app demo surface.

The local mock uses the normal recovery pipeline and records signed synthetic
webhook envelopes. Mock captures remain distinguishable from Razorpay Test API
captures and never contribute to provider-confirmed Razorpay metrics.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.domain.models import NormalizedPaymentEvent, RecoveryExecution
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.razorpay.normalizer import normalize_razorpay_event
from app.recovery.decision_engine import DeterministicDecisionProvider
from app.recovery.execution_provider import RazorpayMockExecutionProvider
from app.recovery.orchestrator import RecoveryOrchestrator

_LAB_SIGNING_SECRET = secrets.token_bytes(32)


class RazorpayTestLab:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        adapter: WaggleRecoveryMemoryAdapter,
        orchestrator: RecoveryOrchestrator,
    ) -> None:
        self.settings = settings
        self.db = db
        self.adapter = adapter
        self.base_orchestrator = orchestrator

    @property
    def real_test_mode_connected(self) -> bool:
        provider = self.base_orchestrator.execution_provider
        return bool(provider and provider.name == "razorpay_test" and provider.configured())

    def configuration(self) -> dict[str, Any]:
        return {
            "mode": "razorpay_test_api" if self.real_test_mode_connected else "local_mock",
            "mode_label": "Connected Razorpay Test API" if self.real_test_mode_connected else "Local Razorpay Mock",
            "real_test_mode_connected": self.real_test_mode_connected,
            "mock_enabled": self.settings.razorpay_mock_lab_enabled,
            "test_mode": True,
            "real_money": False,
            "payment_link_test_limit": 30,
            "capture_authority": (
                "Verified payment.captured from Razorpay"
                if self.real_test_mode_connected
                else "Signed local mock payment.captured (never counted as Razorpay-confirmed GMV)"
            ),
        }

    def create_failure(
        self,
        *,
        amount: int,
        customer_id: str,
        merchant_id: str,
        method: str,
        instrument_id: str,
        failure_code: str,
        failure_description: str,
    ) -> dict[str, Any]:
        if not self.real_test_mode_connected and not self.settings.razorpay_mock_lab_enabled:
            raise ValueError("Local Razorpay mock is disabled and Test API credentials are not configured")
        suffix = uuid4().hex[:12]
        payment_id = f"pay_mock_failed_{suffix}"
        event_id = f"evt_mock_failed_{suffix}"
        payload = self._payment_payload(
            event_type="payment.failed",
            payment_id=payment_id,
            event_id=event_id,
            amount=amount,
            customer_id=customer_id,
            merchant_id=merchant_id,
            method=method,
            instrument_id=instrument_id,
            failure_code=failure_code,
            failure_description=failure_description,
        )
        event = self._accept_mock_webhook(payload)
        orchestrator = self.base_orchestrator if self.real_test_mode_connected else self._mock_orchestrator()
        result = orchestrator.process_event(event=event, simulate=False)
        self.db.mark_webhook_processed(event_id)
        return {
            "status": "created",
            "configuration": self.configuration(),
            "webhook": self._webhook_summary(payload, signature_valid=True, processed=True),
            "result": result,
            "lab_state": self.state(),
        }

    def complete_mock_execution(self, execution_id: str, *, outcome: str, method: str) -> dict[str, Any]:
        execution = self.db.get_execution_for_confirmation(execution_id=execution_id)
        if execution is None:
            raise ValueError("Recovery execution not found")
        if execution["provider"] != "razorpay_mock":
            raise ValueError("Connected Razorpay Test links must be completed on Razorpay Checkout")
        if execution["status"] == "SUCCESS":
            return {"status": "duplicate", "execution": self._safe_execution(execution), "lab_state": self.state()}
        if execution["status"] != "PENDING":
            raise ValueError("Only a pending mock execution can be completed")

        suffix = uuid4().hex[:12]
        success = outcome == "success"
        event_type = "payment.captured" if success else "payment.failed"
        payment_id = f"pay_mock_recovery_{suffix}"
        event_id = f"evt_mock_{'captured' if success else 'failed'}_{suffix}"
        payload = self._payment_payload(
            event_type=event_type,
            payment_id=payment_id,
            event_id=event_id,
            amount=int(execution["amount"]),
            customer_id=str(execution["customer_id"]),
            merchant_id=str(execution["merchant_id"]),
            method=method,
            instrument_id=f"{method}_mock",
            failure_code="mock_checkout_failed" if not success else "",
            failure_description="Customer selected failure in local mock checkout" if not success else "",
            recovery_execution_id=execution_id,
        )
        event = self._accept_mock_webhook(payload)
        if success:
            result = self._mock_orchestrator().process_event(event=event, simulate=False)
            self.db.mark_webhook_processed(event_id)
        else:
            # A failed customer checkout does not terminate the Payment Link or
            # count revenue. Keep the original recovery execution pending.
            self.db.mark_webhook_processed(event_id)
            result = {
                "status": "mock_payment_failed",
                "payment_id": payment_id,
                "execution_id": execution_id,
                "updated_attempts": 0,
                "recovered_amount": 0,
                "confirmation": "NOT CONFIRMED — MOCK PAYMENT FAILED",
            }
        return {
            "status": result["status"],
            "webhook": self._webhook_summary(payload, signature_valid=True, processed=True),
            "result": result,
            "lab_state": self.state(),
        }

    def state(self) -> dict[str, Any]:
        executions = [self._safe_execution(item) for item in self.db.get_recent_executions()]
        return {
            "configuration": self.configuration(),
            "executions": executions,
            "webhooks": self.db.get_recent_webhooks(),
        }

    def _mock_orchestrator(self) -> RecoveryOrchestrator:
        return RecoveryOrchestrator(
            adapter=self.adapter,
            db=self.db,
            settings=self.settings,
            decision_provider=DeterministicDecisionProvider(),
            execution_provider=RazorpayMockExecutionProvider(),
            escalation_handoff=self.base_orchestrator.escalation_handoff,
        )

    def _accept_mock_webhook(self, payload: dict[str, Any]) -> NormalizedPaymentEvent:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(_LAB_SIGNING_SECRET, body, hashlib.sha256).hexdigest()
        expected = hmac.new(_LAB_SIGNING_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Local mock webhook signature verification failed")
        event = normalize_razorpay_event(payload)
        if event is None:
            raise ValueError("Local mock produced an unsupported webhook")
        event.event_id = str(payload["id"])
        event.source = "razorpay_mock"
        event.test_mode = True
        payment_id = str(payload["payload"]["payment"]["entity"]["id"])
        self.db.upsert_webhook_event({
            "id": str(payload["id"]),
            "provider_event_id": str(payload["id"]),
            "event_type": str(payload["event"]),
            "payment_id": payment_id,
            "raw_payload": json.dumps(payload),
            "signature_valid": 1,
            "processed": 0,
            "created_at": datetime.now(UTC).isoformat(),
        })
        return event

    @staticmethod
    def _payment_payload(
        *,
        event_type: str,
        payment_id: str,
        event_id: str,
        amount: int,
        customer_id: str,
        merchant_id: str,
        method: str,
        instrument_id: str,
        failure_code: str,
        failure_description: str,
        recovery_execution_id: str = "",
    ) -> dict[str, Any]:
        failed = event_type == "payment.failed"
        entity: dict[str, Any] = {
            "id": payment_id,
            "order_id": f"order_mock_{payment_id[-12:]}",
            "amount": amount,
            "currency": "INR",
            "status": "failed" if failed else "captured",
            "method": method,
            "merchant_id": merchant_id,
            "notes": {
                "customer_id": customer_id,
                "instrument_id": instrument_id,
                **({"recovery_execution_id": recovery_execution_id} if recovery_execution_id else {}),
            },
            "created_at": int(datetime.now(UTC).timestamp()),
        }
        if failed:
            entity.update({
                "error_code": failure_code,
                "error_description": failure_description,
                "error_source": "mock_issuer",
                "error_step": "payment_authorization",
                "error_reason": failure_code,
            })
        # Match the safe, method-specific fields exposed by Razorpay payment
        # webhooks so the production normalizer is exercised unchanged.
        if method == "card":
            entity["card"] = {"last4": instrument_id[-4:] if instrument_id else "9988"}
        elif method == "upi":
            entity["vpa"] = f"{instrument_id or 'success'}@razorpay"
        elif method == "netbanking":
            entity["bank"] = (instrument_id or "HDFC").removeprefix("nb_").upper()
        elif method == "wallet":
            entity["wallet"] = (instrument_id or "payzapp").removeprefix("wallet_").lower()
        return {"id": event_id, "event": event_type, "payload": {"payment": {"entity": entity}}}

    @staticmethod
    def _webhook_summary(payload: dict[str, Any], *, signature_valid: bool, processed: bool) -> dict[str, Any]:
        entity = payload["payload"]["payment"]["entity"]
        return {
            "provider_event_id": payload["id"],
            "event_type": payload["event"],
            "payment_id": entity["id"],
            "signature_valid": signature_valid,
            "processed": processed,
            "created_at": datetime.fromtimestamp(entity["created_at"], UTC).isoformat(),
        }

    @staticmethod
    def _safe_execution(row: dict[str, Any]) -> dict[str, Any]:
        safe = RecoveryExecution.model_validate(row).safe_dict()
        for field in (
            "failure_id", "customer_id", "merchant_id",
            "failure_code", "failed_method", "action", "recommended_method",
            "retry_after_seconds", "policy_result", "human_review_required",
            "attempt_outcome", "recovered_amount",
        ):
            safe[field] = row.get(field)
        safe["confirmation_label"] = (
            "CONFIRMED BY RAZORPAY WEBHOOK"
            if row["provider"] == "razorpay_test" and row["status"] == "SUCCESS"
            else "CONFIRMED BY LOCAL MOCK WEBHOOK"
            if row["provider"] == "razorpay_mock" and row["status"] == "SUCCESS"
            else "WAITING FOR payment.captured"
        )
        return safe
