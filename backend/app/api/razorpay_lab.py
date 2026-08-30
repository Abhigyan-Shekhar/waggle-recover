"""In-app Razorpay Test Lab APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_mutation_token
from app.config import Settings, get_settings
from app.main import get_adapter, get_db, get_orchestrator
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator
from app.recovery.razorpay_lab import RazorpayTestLab

router = APIRouter()


class LabFailureRequest(BaseModel):
    amount: int = Field(800000, ge=100, le=100000000)
    customer_id: str = Field("CUST-RAZORPAY-LAB", min_length=3, max_length=80)
    merchant_id: str = Field("MERCH-RAZORPAY-LAB", min_length=3, max_length=80)
    method: str = Field("card", pattern="^(card|upi|netbanking|wallet)$")
    instrument_id: str = Field("card_9988", min_length=3, max_length=80)
    failure_code: str = Field("expired_card", min_length=3, max_length=80)
    failure_description: str = Field("Card expired", min_length=3, max_length=200)


class MockCompletionRequest(BaseModel):
    outcome: str = Field(pattern="^(success|failure)$")
    method: str = Field("upi", pattern="^(card|upi|netbanking|wallet)$")


def _lab(
    settings: Settings,
    db: Database,
    adapter: WaggleRecoveryMemoryAdapter,
    orchestrator: RecoveryOrchestrator,
) -> RazorpayTestLab:
    return RazorpayTestLab(settings=settings, db=db, adapter=adapter, orchestrator=orchestrator)


@router.get("/state")
async def lab_state(
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
) -> dict:
    return _lab(settings, db, adapter, orchestrator).state()


@router.post("/failures")
async def create_lab_failure(
    request: LabFailureRequest,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    _authorized: None = Depends(require_mutation_token),
) -> dict:
    try:
        return _lab(settings, db, adapter, orchestrator).create_failure(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/executions/{execution_id}/complete")
async def complete_mock_execution(
    execution_id: str,
    request: MockCompletionRequest,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    _authorized: None = Depends(require_mutation_token),
) -> dict:
    try:
        return _lab(settings, db, adapter, orchestrator).complete_mock_execution(
            execution_id, outcome=request.outcome, method=request.method,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
