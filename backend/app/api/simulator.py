"""Simulator API — fire synthetic payment failures for demo and testing."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import Settings, get_settings
from app.domain.enums import RecoveryAction
from app.domain.models import MerchantPolicy, NormalizedPaymentEvent
from app.main import get_db, get_orchestrator
from app.persistence.database import Database
from app.recovery.decision_engine import DecisionProvider, create_decision_provider
from app.recovery.orchestrator import RecoveryOrchestrator

router = APIRouter()


def _simulator_decision_provider(decision_mode: str, settings: Settings) -> DecisionProvider:
    """Resolve the demo-only mode without mutating the webhook provider singleton."""
    normalized = decision_mode.lower().strip()
    if normalized not in {"deterministic", "agent"}:
        raise HTTPException(status_code=422, detail="decision_mode must be deterministic or agent")
    return create_decision_provider(normalized, settings=settings)

# Canned demo scenarios
DEMO_SCENARIOS = {
    "stale_card_trap": {
        "name": "Stale Card Trap",
        "description": "New card supersedes old card. Old timing memory should be discarded.",
        "steps": [
            {
                "step": 1,
                "action": "register_instrument",
                "alias": "card_legacy",
                "instrument_type": "card",
                "supersedes": None,
            },
            {
                "step": 2,
                "action": "payment_failure",
                "payment_id": "pay_sc_trap_hist1",
                "method": "card",
                "instrument_id": "card_legacy",
                "failure_code": "issuer_unavailable",
                "days_ago": 10,
            },
            {
                "step": 3,
                "action": "recovery_success",
                "payment_id": "pay_sc_trap_hist1_r",
                "method": "card",
                "instrument_id": "card_legacy",
                "retry_after_seconds": 480,
                "days_ago": 10,
            },
            {
                "step": 4,
                "action": "register_instrument",
                "alias": "card_new",
                "instrument_type": "card",
                "supersedes": "card_legacy",
            },
            {
                "step": 5,
                "action": "payment_failure",
                "payment_id": "pay_sc_trap_current",
                "method": "card",
                "instrument_id": "card_new",
                "failure_code": "issuer_unavailable",
                "description": "CURRENT FAILURE — system should NOT use card_legacy timing",
            },
        ],
    },
    "timing_memory": {
        "name": "Timing Memory",
        "description": "System recalls successful retry interval from identical failure pattern.",
        "steps": [],
    },
    "no_history": {
        "name": "No History",
        "description": "Brand new customer — system uses safe defaults with no memory contribution.",
        "steps": [],
    },
    "permanent_failure": {
        "name": "Permanent Failure",
        "description": "Expired card — system should suggest method change, not retry.",
        "steps": [],
    },
}


@router.get("/scenarios")
async def list_scenarios() -> dict[str, Any]:
    return {"scenarios": list(DEMO_SCENARIOS.keys()), "details": DEMO_SCENARIOS}


@router.post("/reset")
async def reset_simulator(
    db: Database = Depends(get_db),
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Reset the isolated simulator tenant and its companion application DB."""
    db.clear_recovery_data()
    cleared = orchestrator.adapter.graph.clear_all()
    return {"status": "reset", "memory": cleared.model_dump(mode="json")}


@router.post("/payment-failure")
async def simulate_payment_failure(
    body: dict[str, Any],
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """
    Simulate a payment failure and run the full recovery pipeline.

    Body:
        payment_id: optional payment ID (auto-generated if absent)
        customer_id: customer identifier
        merchant_id: merchant identifier
        amount: amount in paise
        method: card / upi / netbanking / wallet
        instrument_id: instrument alias
        failure_code: e.g. "issuer_unavailable"
        failure_reason: human-readable reason
        merchant_policy: optional policy override
        simulation_outcomes: optional dict mapping action → outcome
    """
    payment_id = body.get("payment_id") or f"pay_sim_{uuid.uuid4().hex[:10]}"

    event = NormalizedPaymentEvent(
        event_type="payment.failed",
        payment_id=payment_id,
        customer_id=body.get("customer_id", f"CUST_SIM_{uuid.uuid4().hex[:6]}"),
        merchant_id=body.get("merchant_id", "MERCH_SIM_001"),
        amount=int(body.get("amount", 100000)),
        currency=body.get("currency", "INR"),
        method=body.get("method", "card"),
        instrument_id=body.get("instrument_id", "card_1234"),
        error_code=body.get("failure_code", "issuer_unavailable"),
        error_description=body.get("failure_reason", "Issuer temporarily unavailable"),
        error_source=body.get("failure_source", ""),
        error_step=body.get("failure_step", ""),
        created_at=datetime.now(UTC),
        source="simulator",
    )

    # Build optional merchant policy
    merchant_policy = None
    if "merchant_policy" in body:
        pol = body["merchant_policy"]
        merchant_policy = MerchantPolicy(
            merchant_id=event.merchant_id,
            max_recovery_attempts=pol.get("max_recovery_attempts", 3),
            min_retry_interval_seconds=pol.get("min_retry_interval_seconds", 300),
            max_retry_interval_seconds=pol.get("max_retry_interval_seconds", 3600),
            allowed_actions=[RecoveryAction(a) for a in pol.get("allowed_actions", [
                "RETRY_NOW", "RETRY_AFTER", "SUGGEST_METHOD", "CUSTOMER_NUDGE", "STOP"
            ])],
            blocked_methods=pol.get("blocked_methods", []),
        )

    simulation_outcomes = body.get("simulation_outcomes")
    provider = _simulator_decision_provider(str(body.get("decision_mode", "deterministic")), settings)

    result = await asyncio.to_thread(
        orchestrator.process_event,
        event=event,
        merchant_policy=merchant_policy,
        simulation_outcomes=simulation_outcomes,
        simulate=True,
        decision_provider=provider,
    )

    return result


@router.post("/register-instrument")
async def register_instrument(
    body: dict[str, Any],
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Register a payment instrument (optionally superseding an old one)."""
    instrument = orchestrator.register_instrument(
        customer_id=body["customer_id"],
        instrument_type=body.get("instrument_type", "card"),
        alias=body["alias"],
        supersedes_alias=body.get("supersedes_alias"),
    )
    return {
        "status": "registered",
        "instrument": instrument.model_dump(mode="json"),
        "supersession_created": bool(body.get("supersedes_alias")),
    }


@router.post("/run-scenario/{scenario_id}")
async def run_demo_scenario(
    scenario_id: str,
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    decision_mode: str = Query("deterministic"),
) -> dict[str, Any]:
    """Run a canned demo scenario end-to-end."""
    from app.evaluation.generator import ScenarioGenerator

    gen = ScenarioGenerator(seed=42)
    scenarios = gen._curated_scenarios()

    # Map scenario_id to curated scenario
    scenario_map = {s.id: s for s in scenarios}
    named = {s.name.lower().replace(" ", "_"): s for s in scenarios}

    if scenario_id in scenario_map:
        scenario = scenario_map[scenario_id]
    elif scenario_id.lower() in named:
        scenario = named[scenario_id.lower()]
    else:
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not supported via this endpoint")

    # Populate memory
    from app.evaluation.runner import _populate_memory
    _populate_memory(orchestrator.adapter, db, orchestrator, scenario)

    # Run the current failure
    from app.domain.models import NormalizedPaymentEvent
    event = NormalizedPaymentEvent(
        event_type="payment.failed",
        # Retry-limit scenarios intentionally reuse a payment ID so the
        # Policy Guard sees the same recovery episode and its attempt budget.
        payment_id=scenario.current_payment_id or f"demo_{scenario.id}_{uuid.uuid4().hex[:6]}",
        customer_id=scenario.customer_id,
        merchant_id=scenario.merchant_id,
        amount=scenario.amount,
        method=scenario.method,
        instrument_id=scenario.instrument_id,
        error_code=scenario.failure_code,
        error_description=scenario.failure_reason,
        created_at=datetime.now(UTC),
        source="simulator",
    )

    result = await asyncio.to_thread(
        orchestrator.process_event,
        event=event,
        simulation_outcomes=scenario.action_outcomes,
        simulate=True,
        decision_provider=_simulator_decision_provider(decision_mode, settings),
    )

    return {
        "scenario": {
            "id": scenario.id,
            "name": scenario.name,
            "category": scenario.category,
            "has_stale_memory": scenario.has_stale_memory,
            "has_useful_memory": scenario.has_useful_memory,
            "ground_truth_actions": scenario.ground_truth_actions,
        },
        "result": result,
        "correct": result.get("decision", {}).get("action") in scenario.ground_truth_actions,
    }


@router.post("/scenario/{scenario_id}/run")
async def run_scenario_requested_shape(
    scenario_id: str,
    orchestrator: RecoveryOrchestrator = Depends(get_orchestrator),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    decision_mode: str = Query("deterministic"),
) -> dict[str, Any]:
    """Requested public route; delegates to the single simulator implementation."""
    return await run_demo_scenario(scenario_id, orchestrator, db, settings, decision_mode)


@router.get("/scenarios/curated")
async def list_curated_scenarios() -> dict[str, Any]:
    from app.evaluation.generator import ScenarioGenerator
    gen = ScenarioGenerator(seed=42)
    scenarios = gen._curated_scenarios()
    return {
        "scenarios": [
            {
                "id": s.id,
                "name": s.name,
                "category": s.category,
                "has_stale_memory": s.has_stale_memory,
                "has_useful_memory": s.has_useful_memory,
                "ground_truth_actions": s.ground_truth_actions,
                "customer_id": s.customer_id,
                "merchant_id": s.merchant_id,
                "failure_code": s.failure_code,
            }
            for s in scenarios
        ]
    }
