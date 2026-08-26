"""Decisions API — retrieve decision audit trails and explanations."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.main import get_adapter, get_db
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database

router = APIRouter()


@router.get("/{failure_id}")
async def get_decision_for_failure(
    failure_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rows = db.get_decisions_for_failure(failure_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No decisions found")
    for row in rows:
        for field in ("evidence_json", "discarded_json"):
            if field in row and row[field]:
                try:
                    row[field] = json.loads(row[field])
                except Exception:
                    row[field] = []
    return {"data": rows, "count": len(rows)}


@router.get("/{failure_id}/graph")
async def get_decision_memory_graph(
    failure_id: str,
    db: Database = Depends(get_db),
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
) -> dict[str, Any]:
    """Return the Waggle memory subgraph for a decision (for graph visualization)."""
    rows = db.get_decisions_for_failure(failure_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No decisions found")

    decision_row = rows[0]
    waggle_node_id = decision_row.get("waggle_node_id")
    if not waggle_node_id:
        return {"nodes": [], "edges": [], "message": "No Waggle node for this decision"}

    graph_data = adapter.get_nodes_and_edges_for_decision(waggle_node_id)
    return graph_data


@router.get("/{failure_id}/explanation")
async def get_explanation(
    failure_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rows = db.get_decisions_for_failure(failure_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No decisions found")
    decision = rows[0]
    return {
        "failure_id": failure_id,
        "explanation": decision.get("explanation", "No explanation available"),
        "action": decision.get("action"),
        "confidence": decision.get("confidence"),
        "memory_contribution": decision.get("memory_contribution"),
    }
