"""Safe recovery execution status APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.domain.models import RecoveryExecution
from app.main import get_db
from app.persistence.database import Database

router = APIRouter()


@router.get("/decision/{decision_id}")
async def execution_for_decision(decision_id: str, db: Database = Depends(get_db)) -> dict:
    row = db.get_execution_for_decision(decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No recovery execution exists for this decision")
    return RecoveryExecution.model_validate(row).safe_dict()
