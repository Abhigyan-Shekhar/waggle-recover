"""Merchant policy console backed exclusively by versioned Waggle nodes."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_mutation_token
from app.domain.enums import RecoveryAction
from app.domain.models import MerchantPolicy
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.recovery.orchestrator import RecoveryOrchestrator

router = APIRouter()


def get_adapter_dependency() -> WaggleRecoveryMemoryAdapter:
    from app.main import get_adapter

    return get_adapter()


def get_orchestrator_dependency() -> RecoveryOrchestrator:
    from app.main import get_orchestrator

    return get_orchestrator()


class PolicyUpdate(BaseModel):
    max_recovery_attempts: int | None = None
    min_retry_interval_seconds: int | None = None
    max_retry_interval_seconds: int | None = None
    allowed_actions: list[RecoveryAction] | None = None
    blocked_methods: list[str] | None = None
    blocked_routes: list[str] | None = None
    requires_human_review: bool | None = None
    requires_human_review_below_confidence: bool | None = None
    min_automatic_confidence: float | None = None


def _history(adapter: WaggleRecoveryMemoryAdapter, merchant_id: str) -> list[dict]:
    return [
        {
            "node_id": item["id"],
            **item.get("metadata", {}),
            "current": not bool(item.get("valid_to")),
            "valid_to": item.get("valid_to"),
        }
        for item in adapter.get_merchant_policy_history(merchant_id)
    ]


@router.get("/{merchant_id}")
async def get_policy(
    merchant_id: str,
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter_dependency),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator_dependency),
) -> dict:
    return {
        "current": orchestrator._load_merchant_policy(merchant_id).model_dump(mode="json"),
        "history": _history(adapter, merchant_id),
    }


@router.post("/{merchant_id}")
async def update_policy(
    merchant_id: str,
    update: PolicyUpdate,
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter_dependency),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator_dependency),
    _authorized: None = Depends(require_mutation_token),
) -> dict:
    current_node = adapter.get_merchant_policy_node(merchant_id)
    current = orchestrator._load_merchant_policy(merchant_id)
    changes = update.model_dump(exclude_none=True)
    policy = MerchantPolicy(
        **{
            **current.model_dump(),
            **changes,
            "policy_id": MerchantPolicy(merchant_id=merchant_id).policy_id,
            "merchant_id": merchant_id,
            "version": int(current_node.get("metadata", {}).get("version", 0)) + 1 if current_node else 1,
            "effective_from": datetime.now(UTC),
            "supersedes_policy_id": current.policy_id if current_node else None,
        }
    )
    node_id = adapter.store_merchant_policy(policy)
    return {
        "status": "updated",
        "node_id": node_id,
        "current": policy.model_dump(mode="json"),
        "history": _history(adapter, merchant_id),
    }
