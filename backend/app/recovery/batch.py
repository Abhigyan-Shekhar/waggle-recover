"""Merchant batch orchestration through the normal per-case pipeline."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.domain.enums import OutcomeStatus, PolicyResult, RecoveryAction
from app.domain.models import BatchRecoveryCase, NormalizedPaymentEvent, RecoveryBatch
from app.evaluation.ablations import _is_inherently_unsafe
from app.evaluation.generator import ScenarioGenerator, isolate_demo_run
from app.evaluation.runner import _populate_memory
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator


def run_curated_batch(
    orchestrator: RecoveryOrchestrator,
    db: Database,
    *,
    count: int = 25,
    seed: int = 42,
) -> dict:
    if count < 20 or count > 50:
        raise ValueError("Batch size must be between 20 and 50 cases")
    merchant_id = f"MERCH-BATCH-{seed}"
    batch = RecoveryBatch(merchant_id=merchant_id, case_count=count)
    db.upsert_batch(batch.model_dump(mode="json"))
    scenarios = ScenarioGenerator(seed=seed).generate(count)
    for index, raw in enumerate(scenarios):
        scenario = isolate_demo_run(raw, f"{batch.id[-6:]}-{index:02d}")
        scenario = replace(
            scenario,
            merchant_id=merchant_id,
            history=[replace(item, merchant_id=merchant_id) for item in scenario.history],
            merchant_policies=[{**policy, "merchant_id": merchant_id} for policy in scenario.merchant_policies],
        )
        _populate_memory(orchestrator.adapter, db, orchestrator, scenario)
        event = NormalizedPaymentEvent(
            event_type="payment.failed", payment_id=scenario.current_payment_id or f"pay_{batch.id[-8:]}_{index:02d}",
            customer_id=scenario.customer_id, merchant_id=merchant_id, amount=scenario.amount,
            method=scenario.method, instrument_id=scenario.instrument_id, error_code=scenario.failure_code,
            error_description=scenario.failure_reason, created_at=datetime.now(UTC), source="batch_simulator",
        )
        result = orchestrator.process_event(
            event=event, simulation_outcomes=scenario.action_outcomes, simulate=True,
        )
        decision = result["decision"]
        action = RecoveryAction(decision["action"])
        outcome = OutcomeStatus(result["outcome"]["outcome"])
        case = BatchRecoveryCase(
            batch_id=batch.id, failure_id=result["failure_id"],
            recovery_episode_id=result["recovery_episode"]["id"], amount=scenario.amount,
            action=action, outcome=outcome, risk_score=int(decision.get("risk_score", 0)),
            risk_band=str(decision.get("risk_band", "LOW")),
            stale_evidence_rejected=int(result["metrics"]["evidence_discarded"]),
            policy_blocked=decision.get("policy_result") == PolicyResult.BLOCK.value,
            unsafe_action=_is_inherently_unsafe(scenario, decision, False),
            policy_violation=False,
        )
        db.insert_batch_case(case.model_dump(mode="json"))
    batch.status = "COMPLETED"
    batch.completed_at = datetime.now(UTC)
    db.upsert_batch(batch.model_dump(mode="json"))
    return db.get_batch(batch.id) or {}
