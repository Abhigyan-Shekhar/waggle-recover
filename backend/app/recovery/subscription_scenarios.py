"""Curated subscription/mandate failures using the full recovery architecture."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.domain.models import MerchantPolicy, RevenueRiskEvent
from app.evaluation.generator import EvalScenario, ScenarioGenerator
from app.evaluation.runner import _populate_memory
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator
from app.recovery.revenue_risk import normalize_revenue_risk


def curated_subscription_scenarios() -> dict[str, EvalScenario]:
    curated = ScenarioGenerator(seed=42)._curated_scenarios()
    return {
        "mandate_timing_memory": replace(
            curated[1], name="Mandate Timing Memory", category="subscription_timing_memory"
        ),
        "mandate_instrument_replaced": replace(
            curated[2], name="Mandate Instrument Replaced", category="subscription_stale_instrument"
        ),
        "mandate_escalation": replace(
            curated[5], name="Mandate Escalation Required", category="subscription_escalation",
            history=[], current_payment_id=None,
        ),
        "mandate_no_memory": replace(
            curated[4], name="Mandate No Authoritative Memory", category="subscription_no_memory"
        ),
    }


def run_subscription_scenario(
    scenario_id: str,
    orchestrator: RecoveryOrchestrator,
    db: Database,
) -> dict[str, Any]:
    scenarios = curated_subscription_scenarios()
    if scenario_id not in scenarios:
        raise KeyError(scenario_id)
    run_token = uuid4().hex[:8]
    original = scenarios[scenario_id]
    customer_id = f"{original.customer_id}-S{run_token}"
    scenario = replace(
        original,
        customer_id=customer_id,
        history=[replace(item, customer_id=customer_id, payment_id=f"{item.payment_id}-{run_token}") for item in original.history],
        current_payment_id=(f"{original.current_payment_id}-{run_token}" if original.current_payment_id else None),
    )
    mandate_id = f"mandate_{scenario_id}_{run_token}"
    risk_event = RevenueRiskEvent(
        risk_type="SUBSCRIPTION_FAILURE",
        event_id=f"evt_{scenario_id}_{run_token}",
        payment_id=scenario.current_payment_id or f"pay_{scenario_id}_{run_token}",
        subscription_id=f"sub_{run_token}",
        mandate_id=mandate_id,
        customer_id=scenario.customer_id,
        merchant_id=scenario.merchant_id,
        amount=scenario.amount,
        method=scenario.method,
        instrument_id=scenario.instrument_id,
        failure_code=scenario.failure_code,
        failure_reason=scenario.failure_reason,
        created_at=datetime.now(UTC),
        test_mode=True,
    )
    normalized = normalize_revenue_risk(risk_event)

    if scenario_id == "mandate_escalation":
        # This proof exercises actual budget exhaustion. With a two-attempt
        # policy the provider originates ESCALATE at retry_count=2; no STOP is
        # rewritten to manufacture the handoff.
        escalation_policy = MerchantPolicy(
            merchant_id=scenario.merchant_id,
            max_recovery_attempts=2,
        )
        results = [
            orchestrator.process_event(
                event=normalized,
                merchant_policy=escalation_policy,
                simulation_outcomes=scenario.action_outcomes,
                simulate=True,
            )
            for _ in range(4)
        ]
        result = results[-1]
    else:
        _populate_memory(orchestrator.adapter, db, orchestrator, scenario)
        result = orchestrator.process_event(
            event=normalized,
            simulation_outcomes=scenario.action_outcomes,
            simulate=True,
        )

    return {
        "risk_type": "SUBSCRIPTION_FAILURE",
        "scenario": {
            "id": scenario_id,
            "name": scenario.name,
            "category": scenario.category,
            "mandate_id": mandate_id,
            "has_stale_memory": scenario.has_stale_memory,
        },
        "result": result,
    }
