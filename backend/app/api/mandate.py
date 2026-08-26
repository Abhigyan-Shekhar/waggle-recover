"""Mandate recovery advice API (does not control NPCI/bank retry rails)."""
from fastapi import APIRouter

from app.domain.models import MandateContext
from app.recovery.mandate import recommend_mandate_recovery

router = APIRouter()


@router.post("/recommend")
async def recommend(context: MandateContext) -> dict[str, object]:
    return recommend_mandate_recovery(context)
