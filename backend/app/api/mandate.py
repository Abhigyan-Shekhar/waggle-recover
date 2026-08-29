"""Mandate recovery advice API (does not control NPCI/bank retry rails)."""
from fastapi import APIRouter, Depends, HTTPException

from app.domain.models import MandateContext
from app.main import get_db, get_orchestrator
from app.persistence.database import Database
from app.recovery.mandate import recommend_mandate_recovery
from app.recovery.orchestrator import RecoveryOrchestrator
from app.recovery.subscription_scenarios import curated_subscription_scenarios, run_subscription_scenario

router = APIRouter()


@router.post("/recommend")
async def recommend(context: MandateContext) -> dict[str, object]:
    return recommend_mandate_recovery(context)


@router.get("/scenarios")
async def list_scenarios() -> dict[str, object]:
    scenarios = curated_subscription_scenarios()
    return {
        "risk_type": "SUBSCRIPTION_FAILURE",
        "scenarios": [
            {"id": key, "name": value.name, "category": value.category}
            for key, value in scenarios.items()
        ],
    }


@router.post("/scenarios/{scenario_id}/run")
async def run_scenario(
    scenario_id: str,
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    try:
        return run_subscription_scenario(scenario_id, orchestrator, db)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown subscription recovery scenario") from None
