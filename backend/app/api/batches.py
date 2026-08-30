"""Batch recovery control-room APIs."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_mutation_token
from app.main import get_db, get_orchestrator
from app.persistence.database import Database
from app.recovery.batch import run_curated_batch
from app.recovery.orchestrator import RecoveryOrchestrator

router = APIRouter()


@router.post("/demo")
async def create_demo_batch(
    count: int = Query(25, ge=20, le=50),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    db: Database = Depends(get_db),
    _authorized: None = Depends(require_mutation_token),
) -> dict:
    return await asyncio.to_thread(run_curated_batch, orchestrator, db, count=count)


@router.get("/{batch_id}")
async def get_batch(batch_id: str, db: Database = Depends(get_db)) -> dict:
    result = db.get_batch(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recovery batch not found")
    return result
