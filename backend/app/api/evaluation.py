"""Evaluation API — trigger and fetch evaluation runs."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends

from app.config import Settings, get_settings
from app.main import get_db
from app.persistence.database import Database

router = APIRouter()


@router.post("/run")
async def start_evaluation(
    body: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Start an evaluation run in the background."""
    seed = int(body.get("seed", 42))
    count = int(body.get("count", 200))
    run_id = str(uuid.uuid4())

    run_record = {
        "id": run_id,
        "seed": seed,
        "scenario_count": count,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
        "results_json": None,
        "summary_json": None,
    }
    db.upsert_evaluation_run(run_record)

    background_tasks.add_task(
        _run_evaluation_task,
        run_id=run_id,
        seed=seed,
        count=count,
        db=db,
        settings=settings,
    )

    return {"run_id": run_id, "status": "running", "seed": seed, "count": count}


async def _run_evaluation_task(
    run_id: str,
    seed: int,
    count: int,
    db: Database,
    settings: Settings,
) -> None:
    from app.evaluation.runner import run_evaluation
    try:
        summary = run_evaluation(seed=seed, scenario_count=count, settings=settings)
        db.upsert_evaluation_run({
            "id": run_id,
            "seed": seed,
            "scenario_count": count,
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "results_json": None,
            "summary_json": json.dumps(summary.to_dict()),
        })
    except Exception as e:
        db.upsert_evaluation_run({
            "id": run_id,
            "seed": seed,
            "scenario_count": count,
            "status": "failed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "results_json": None,
            "summary_json": json.dumps({"error": str(e)}),
        })


@router.get("/runs")
async def list_runs(db: Database = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute("SELECT * FROM evaluation_runs ORDER BY started_at DESC LIMIT 20")
    result = []
    for row in rows:
        r = dict(row)
        if r.get("summary_json"):
            try:
                r["summary"] = json.loads(r["summary_json"])
            except Exception:
                r["summary"] = None
        result.append(r)
    return {"data": result, "count": len(result)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    row = db.execute_one("SELECT * FROM evaluation_runs WHERE id = ?", (run_id,))
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Run not found")
    r = dict(row)
    if r.get("summary_json"):
        try:
            r["summary"] = json.loads(r["summary_json"])
        except Exception:
            r["summary"] = None
    return r


@router.post("/run-sync")
async def run_evaluation_sync(
    body: dict[str, Any],
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Run evaluation synchronously (for small counts). Use for demo."""
    from app.evaluation.runner import run_evaluation
    seed = int(body.get("seed", 42))
    count = int(body.get("count", 20))  # Keep small for sync
    summary = run_evaluation(seed=seed, scenario_count=min(count, 50), settings=settings)
    return summary.to_dict()
