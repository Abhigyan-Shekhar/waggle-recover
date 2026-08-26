"""Payments API — dashboard data and failure history."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends

from app.main import get_db, get_orchestrator
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator

router = APIRouter()


@router.get("/")
async def list_recoveries(
    limit: int = 100,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rows = db.get_all_recoveries(limit=limit)
    # Parse JSON fields
    for row in rows:
        for field in ("evidence_json", "discarded_json"):
            if field in row and row[field]:
                try:
                    row[field] = json.loads(row[field])
                except Exception:
                    row[field] = []
    return {"data": rows, "count": len(rows)}


@router.get("/overview")
async def overview_metrics(db: Database = Depends(get_db)) -> dict[str, Any]:
    return db.get_overview_metrics()


@router.get("/{failure_id}")
async def get_recovery(
    failure_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    row = db.get_recovery_by_failure_id(failure_id)
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Failure not found")

    for field in ("evidence_json", "discarded_json"):
        if field in row and row[field]:
            try:
                row[field] = json.loads(row[field])
            except Exception:
                row[field] = []
    return row


@router.get("/{failure_id}/decisions")
async def get_decisions(
    failure_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rows = db.get_decisions_for_failure(failure_id)
    return {"data": rows, "count": len(rows)}


@router.get("/{failure_id}/attempts")
async def get_attempts(
    failure_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rows = db.get_attempts_for_failure(failure_id)
    return {"data": rows, "count": len(rows)}


@router.post("/instruments")
async def register_instrument(
    body: dict[str, Any],
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    instrument = orchestrator.register_instrument(
        customer_id=body["customer_id"],
        instrument_type=body["instrument_type"],
        alias=body["alias"],
        supersedes_alias=body.get("supersedes_alias"),
    )
    return instrument.model_dump(mode="json")
