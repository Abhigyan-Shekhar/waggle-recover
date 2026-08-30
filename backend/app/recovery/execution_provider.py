"""Bounded recovery execution providers.

Providers expose a customer-completed surface. They never charge a stored
instrument and never claim success before a verified provider confirmation.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.domain.models import RecoveryExecution


class RecoveryExecutionProvider(ABC):
    name = "unknown"

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def create(self, execution: RecoveryExecution) -> RecoveryExecution: ...

    def resolve_payment_link_id(self, payment_id: str) -> str | None:
        return None


class SimulationExecutionProvider(RecoveryExecutionProvider):
    name = "simulation"

    def configured(self) -> bool:
        return True

    def create(self, execution: RecoveryExecution) -> RecoveryExecution:
        return execution.model_copy(update={"provider": self.name, "status": "PENDING", "provider_status": "simulated"})


class RazorpayTestExecutionProvider(RecoveryExecutionProvider):
    """Creates only Razorpay Test Mode Payment Links using test credentials."""

    name = "razorpay_test"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client

    def configured(self) -> bool:
        return bool(
            self.settings.razorpay_enabled
            and self.settings.razorpay_test_execution_enabled
            and self.settings.razorpay_key_id.startswith("rzp_test_")
            and self.settings.razorpay_key_secret
            and self.settings.razorpay_webhook_secret
        )

    def create(self, execution: RecoveryExecution) -> RecoveryExecution:
        if not self.configured():
            raise RuntimeError("Razorpay Test Mode execution is not configured with test credentials")
        expiry = datetime.now(UTC) + timedelta(seconds=max(300, self.settings.razorpay_payment_link_expiry_seconds))
        reference = f"wr_{hashlib.sha256(execution.id.encode()).hexdigest()[:30]}"
        payload = {
            "amount": execution.amount,
            "currency": execution.currency,
            "accept_partial": False,
            "expire_by": int(expiry.timestamp()),
            "reference_id": reference,
            "description": "Waggle Recover test-mode recovery",
            "notes": {
                "recovery_execution_id": execution.id,
                "recovery_episode_id": execution.recovery_episode_id,
                "decision_id": execution.decision_id,
                "failure_id": execution.failure_id,
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        response = self._request("POST", "/payment_links", json=payload)
        data = response.json()
        provider_id = str(data.get("id", ""))
        public_url = str(data.get("short_url", ""))
        if not provider_id.startswith("plink_") or not public_url.startswith("https://"):
            raise RuntimeError("Razorpay returned an invalid Payment Link response")
        return execution.model_copy(update={
            "provider": self.name,
            "status": "PENDING",
            "provider_execution_id": provider_id,
            "public_url": public_url,
            "provider_status": str(data.get("status", "created")),
            "updated_at": datetime.now(UTC),
        })

    def resolve_payment_link_id(self, payment_id: str) -> str | None:
        if not self.configured() or not payment_id.startswith("pay_"):
            return None
        response = self._request("GET", "/payment_links", params={"payment_id": payment_id})
        items = response.json().get("payment_links", [])
        if not isinstance(items, list) or len(items) != 1:
            return None
        provider_id = str(items[0].get("id", ""))
        return provider_id if provider_id.startswith("plink_") else None

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self.client or httpx.Client(timeout=10.0)
        close = self.client is None
        try:
            response = client.request(
                method,
                f"{self.settings.razorpay_api_base_url.rstrip('/')}{path}",
                auth=(self.settings.razorpay_key_id, self.settings.razorpay_key_secret),
                headers={"content-type": "application/json"},
                **kwargs,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Razorpay Test Mode request failed ({type(exc).__name__})") from exc
        finally:
            if close:
                client.close()


def build_execution_provider(settings: Settings) -> RecoveryExecutionProvider | None:
    provider = RazorpayTestExecutionProvider(settings)
    return provider if provider.configured() else None
