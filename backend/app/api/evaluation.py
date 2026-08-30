"""Evaluation API — trigger and fetch evaluation runs."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.api.auth import require_mutation_token
from app.config import Settings, get_settings
from app.main import get_db
from app.persistence.database import Database

router = APIRouter()
REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "evaluations"


@router.post("/run")
async def start_evaluation(
    body: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _authorized: None = Depends(require_mutation_token),
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
    """Run blocking graph/evaluation work off the ASGI event loop."""
    await asyncio.to_thread(_run_evaluation_sync_task, run_id, seed, count, db, settings)


def _run_evaluation_sync_task(
    run_id: str,
    seed: int,
    count: int,
    db: Database,
    settings: Settings,
) -> None:
    from app.evaluation.runner import run_evaluation
    try:
        def persist_result(row: dict[str, Any]) -> None:
            db.insert_evaluation_result({
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "scenario_id": row["scenario_id"],
                "system": row["system"],
                "action_taken": row["action_taken"],
                "action_correct": int(row["action_correct"]),
                "recovered_amount": row["recovered_amount"],
                "latency_ms": row["latency_ms"],
                "memory_contribution": row["memory_contribution"],
                "retrieval_mode": row["retrieval_mode"],
                "stale_evidence_detected": int(row["stale_evidence_detected"]),
                "stale_evidence_correctly_rejected": int(row["stale_evidence_correctly_rejected"]),
                "evidence_count": row["evidence_count"],
                "discarded_count": row["discarded_count"],
                "decision_json": json.dumps(row["decision"]),
                "created_at": datetime.now(UTC).isoformat(),
            })

        summary = run_evaluation(seed=seed, scenario_count=count, settings=settings, result_sink=persist_result)
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


@router.get("/runs/{run_id}/results")
async def get_run_results(run_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        "SELECT * FROM evaluation_results WHERE run_id = ? ORDER BY scenario_id, system",
        (run_id,),
    )
    data = []
    for row in rows:
        item = dict(row)
        try:
            item["decision"] = json.loads(item.pop("decision_json"))
        except Exception:
            item["decision"] = {}
        data.append(item)
    return {"data": data, "count": len(data)}


@router.post("/run-sync")
async def run_evaluation_sync(
    body: dict[str, Any],
    settings: Settings = Depends(get_settings),
    _authorized: None = Depends(require_mutation_token),
) -> dict[str, Any]:
    """Run evaluation synchronously (for small counts). Use for demo."""
    from app.evaluation.runner import run_evaluation
    seed = int(body.get("seed", 42))
    count = int(body.get("count", 20))  # Keep small for sync
    summary = run_evaluation(seed=seed, scenario_count=min(count, 50), settings=settings)
    return summary.to_dict()


@router.get("/reports")
async def evaluation_reports() -> dict[str, Any]:
    """Return cached, separately labeled evaluation reports; never trigger spend."""
    reports: dict[str, Any] = {}
    for key, filename in {
        "main": "main.json",
        "robustness": "robustness.json",
        "ablations": "ablations.json",
        "qwen": "qwen.json",
    }.items():
        path = REPORT_DIR / filename
        if not path.exists():
            reports[key] = {
                "status": "not_run",
                "message": "No cached report is available. This evaluation is not run automatically.",
            }
            continue
        try:
            reports[key] = {"status": "completed", "report": json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError):
            reports[key] = {"status": "invalid_cache", "message": "Cached report could not be read."}
    return {"reports": reports}


@router.get("/authority-shadow/{scenario_id}")
async def authority_shadow(scenario_id: str) -> dict[str, Any]:
    """Compare one isolated case; never write a production RecoveryAttempt."""
    from app.evaluation.shadow import run_authority_shadow
    try:
        result = await asyncio.to_thread(run_authority_shadow, scenario_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    cache = REPORT_DIR / "ablations.json"
    result["cached_ablation"] = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
    return result
