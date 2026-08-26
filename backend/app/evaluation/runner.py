"""Evaluation runner — compares Baseline A, Baseline B, and System C (Waggle Recover).

Populates Waggle memory with historical context from each scenario before running
the recovery pipeline. Ensures System C actually uses memory, not just heuristics.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.domain.models import NormalizedPaymentEvent, PaymentInstrument
from app.evaluation.baselines import BlindFixedRetryBaseline, ContextualHistoryBaseline
from app.evaluation.generator import EvalScenario, ScenarioGenerator, ScenarioHistory
from app.evaluation.metrics import ComparisonSummary, SystemMetrics
from app.memory.supersession import SupersessionValidator
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator

LOGGER = logging.getLogger(__name__)


def run_evaluation(
    seed: int = 42,
    scenario_count: int = 200,
    db_path: str | None = None,
    waggle_db_path: str | None = None,
    settings: Settings | None = None,
) -> ComparisonSummary:
    """
    Run full evaluation: load scenarios, populate Waggle memory, evaluate all three systems.

    Returns ComparisonSummary with metrics for all systems.
    """
    settings = settings or get_settings()

    # Use ephemeral DBs for evaluation if not specified
    if db_path is None:
        run_id = str(uuid.uuid4())[:8]
        db_path = str(Path(settings.app_db_abs_path.parent / f"eval_{run_id}.db"))
    if waggle_db_path is None:
        run_id = str(uuid.uuid4())[:8]
        waggle_db_path = str(Path(settings.waggle_db_abs_path.parent / f"eval_waggle_{run_id}.db"))

    # Initialize components
    from waggle.embeddings import EmbeddingModel
    from waggle.graph import MemoryGraph

    graph = MemoryGraph(
        db_path=waggle_db_path,
        embedding_model=EmbeddingModel(settings.waggle_embedding_model),
    )
    tenant_graph = graph.for_tenant(settings.waggle_tenant_id)
    adapter = WaggleRecoveryMemoryAdapter(tenant_graph)
    db = Database(db_path)

    orchestrator = RecoveryOrchestrator(
        adapter=adapter,
        db=db,
        settings=settings,
    )

    baseline_a = BlindFixedRetryBaseline()
    baseline_b = ContextualHistoryBaseline()

    # Generate scenarios
    LOGGER.info("Generating %d evaluation scenarios (seed=%d)...", scenario_count, seed)
    generator = ScenarioGenerator(seed=seed)
    scenarios = generator.generate(scenario_count)
    LOGGER.info("Generated %d scenarios", len(scenarios))

    # Initialize metrics
    metrics_a = SystemMetrics(name="Baseline A: Blind Fixed Retry")
    metrics_b = SystemMetrics(name="Baseline B: Contextual History")
    metrics_c = SystemMetrics(name="System C: Waggle Recover")

    for i, scenario in enumerate(scenarios):
        LOGGER.info("[%d/%d] Running scenario: %s", i + 1, len(scenarios), scenario.name)

        # Each benchmark case is an independent world. Reusing the same
        # customer/instrument pools across cases must never leak memory or
        # retry counts from an earlier case into System C.
        if i:
            db.clear_recovery_data()
            adapter.graph.clear_all()

        # Step 1: Populate Waggle memory with scenario history
        _populate_memory(adapter, db, orchestrator, scenario)

        # Step 2: Evaluate Baseline A
        _eval_baseline_a(scenario, baseline_a, metrics_a)

        # Step 3: Evaluate Baseline B
        _eval_baseline_b(scenario, baseline_b, metrics_b)

        # Step 4: Evaluate System C (Waggle Recover)
        _eval_system_c(scenario, orchestrator, metrics_c)

    summary = ComparisonSummary(
        baseline_a=metrics_a,
        baseline_b=metrics_b,
        system_c=metrics_c,
        scenario_count=len(scenarios),
    )

    LOGGER.info("%s", summary.format_table())
    return summary


def _populate_memory(
    adapter: WaggleRecoveryMemoryAdapter,
    db: Database,
    orchestrator: RecoveryOrchestrator,
    scenario: EvalScenario,
) -> None:
    """Populate Waggle with historical context for a scenario."""
    # Outcomes need a real application failure ID (SQLite enforces the foreign
    # key), while generator event IDs intentionally model separate gateway
    # attempts. Keep the latest failure context per instrument for that link.
    failure_context: dict[tuple[str, str, str], tuple[str, str]] = {}
    # Register instruments
    prev_alias = None
    for instr in scenario.instruments:
        alias = instr["alias"]
        itype = instr["type"]
        supersedes = instr.get("supersedes")

        old_node_id = None
        if supersedes:
            old_node = adapter.get_instrument_node(supersedes, scenario.customer_id)
            if old_node:
                old_node_id = old_node["id"]

        instrument = PaymentInstrument(
            customer_id=scenario.customer_id,
            instrument_type=itype,
            fingerprint_or_safe_alias=alias,
            status=instr.get("status", "active"),
            supersedes_instrument_id=supersedes,
        )
        waggle_node_id = adapter.store_payment_instrument(
            instrument=instrument,
            old_instrument_node_id=old_node_id,
        )
        instrument.waggle_node_id = waggle_node_id

        # Save to app DB
        instr_dict = instrument.model_dump(mode="json")
        instr_dict["waggle_node_id"] = waggle_node_id
        db.upsert_instrument(instr_dict)

        prev_alias = alias

    # Store historical events as Waggle nodes
    for hist in scenario.history:
        if hist.event_type == "instrument_added":
            continue  # Already handled above

        if hist.event_type == "failure":
            event = NormalizedPaymentEvent(
                event_type="payment.failed",
                payment_id=hist.payment_id,
                customer_id=hist.customer_id,
                merchant_id=hist.merchant_id,
                amount=hist.amount,
                method=hist.method,
                instrument_id=hist.instrument_id,
                error_code=hist.failure_code,
                error_description=hist.failure_code,
                created_at=hist.timestamp,
            )
            from app.domain.enums import classify_failure
            from app.domain.models import PaymentFailure
            failure = PaymentFailure(
                external_payment_id=hist.payment_id,
                customer_id=hist.customer_id,
                merchant_id=hist.merchant_id,
                amount=hist.amount,
                method=hist.method,
                instrument_id=hist.instrument_id,
                failure_code=hist.failure_code,
                failure_reason=hist.failure_code,
                occurred_at=hist.timestamp,
            )
            waggle_node_id = adapter.store_payment_failure(failure)
            failure_dict = failure.model_dump(mode="json")
            failure_dict["waggle_node_id"] = waggle_node_id
            failure_dict["created_at"] = datetime.now(UTC).isoformat()
            db.upsert_failure(failure_dict)
            failure_context[(hist.customer_id, hist.merchant_id, hist.instrument_id)] = (
                failure.id,
                waggle_node_id,
            )

        elif hist.event_type == "success":
            from app.domain.models import RecoveryAttempt
            from app.domain.enums import OutcomeStatus, RecoveryAction
            from app.domain.models import PaymentFailure

            key = (hist.customer_id, hist.merchant_id, hist.instrument_id)
            context = failure_context.get(key)
            if context is None:
                # Some merchant-level histories begin with a recorded success.
                # Create a minimal observable failure context, never a synthetic
                # success outcome, so persistence and graph provenance stay valid.
                failure = PaymentFailure(
                    external_payment_id=f"context_{hist.payment_id}",
                    customer_id=hist.customer_id,
                    merchant_id=hist.merchant_id,
                    amount=hist.amount,
                    method=hist.method,
                    instrument_id=hist.instrument_id,
                    failure_code="historical_recovery_context",
                    failure_reason="Historical recovery context",
                    occurred_at=hist.timestamp,
                )
                failure_node_id = adapter.store_payment_failure(failure)
                failure_dict = failure.model_dump(mode="json")
                failure_dict["waggle_node_id"] = failure_node_id
                failure_dict["created_at"] = datetime.now(UTC).isoformat()
                db.upsert_failure(failure_dict)
                context = (failure.id, failure_node_id)
                failure_context[key] = context
            attempt = RecoveryAttempt(
                failure_id=context[0],
                customer_id=hist.customer_id,
                merchant_id=hist.merchant_id,
                action_type=RecoveryAction(hist.action_taken) if hist.action_taken else RecoveryAction.RETRY_AFTER,
                recommended_method=hist.method,
                retry_after_seconds=hist.retry_after_seconds,
                executed_at=hist.timestamp,
                outcome=OutcomeStatus.SUCCESS,
                recovered_amount=hist.amount,
            )
            waggle_node_id = adapter.store_recovery_outcome(attempt, failure_node_id=context[1])
            attempt_dict = attempt.model_dump(mode="json")
            attempt_dict["waggle_outcome_node_id"] = waggle_node_id
            db.upsert_attempt(attempt_dict)


def _eval_baseline_a(
    scenario: EvalScenario,
    baseline: BlindFixedRetryBaseline,
    metrics: SystemMetrics,
) -> None:
    start = time.time()
    decision = baseline.decide(scenario)
    latency_ms = (time.time() - start) * 1000

    _update_metrics(scenario, decision.action.value, decision, metrics, latency_ms)


def _eval_baseline_b(
    scenario: EvalScenario,
    baseline: ContextualHistoryBaseline,
    metrics: SystemMetrics,
) -> None:
    start = time.time()
    decision = baseline.decide(scenario)
    latency_ms = (time.time() - start) * 1000

    _update_metrics(scenario, decision.action.value, decision, metrics, latency_ms)


def _eval_system_c(
    scenario: EvalScenario,
    orchestrator: RecoveryOrchestrator,
    metrics: SystemMetrics,
) -> None:
    event = NormalizedPaymentEvent(
        event_type="payment.failed",
        payment_id=f"eval_{scenario.id}_current",
        customer_id=scenario.customer_id,
        merchant_id=scenario.merchant_id,
        amount=scenario.amount,
        method=scenario.method,
        instrument_id=scenario.instrument_id,
        error_code=scenario.failure_code,
        error_description=scenario.failure_reason,
        created_at=datetime.now(UTC),
    )

    start = time.time()
    result = orchestrator.process_event(
        event=event,
        simulation_outcomes=scenario.action_outcomes,
        simulate=True,
    )
    latency_ms = (time.time() - start) * 1000

    if result.get("status") != "processed":
        metrics.scenario_count += 1
        return

    decision = result["decision"]
    outcome = result["outcome"]
    eval_metrics = result.get("metrics", {})

    action = decision["action"]
    _update_metrics_dict(scenario, action, decision, outcome, eval_metrics, metrics, latency_ms)


def _update_metrics(
    scenario: EvalScenario,
    action: str,
    decision: Any,
    metrics: SystemMetrics,
    latency_ms: float,
) -> None:
    metrics.scenario_count += 1
    metrics.total_amount_at_risk += scenario.amount
    metrics.decision_latencies.append(latency_ms)

    # Is the action correct?
    correct = action in scenario.ground_truth_actions
    if correct:
        metrics.correct_action_count += 1

    # Simulate outcome
    outcome_str = _outcome_for_decision(scenario, action, decision)
    if outcome_str == "SUCCESS":
        metrics.success_count += 1
        metrics.total_recovered_amount += scenario.amount

    # Category tracking
    cat = scenario.category
    if cat not in metrics.category_results:
        metrics.category_results[cat] = {"total": 0, "correct": 0, "success": 0}
    metrics.category_results[cat]["total"] += 1
    if correct:
        metrics.category_results[cat]["correct"] += 1
    if outcome_str == "SUCCESS":
        metrics.category_results[cat]["success"] += 1


def _update_metrics_dict(
    scenario: EvalScenario,
    action: str,
    decision: dict,
    outcome: dict,
    eval_metrics: dict,
    metrics: SystemMetrics,
    latency_ms: float,
) -> None:
    metrics.scenario_count += 1
    metrics.total_amount_at_risk += scenario.amount
    metrics.decision_latencies.append(latency_ms)

    correct = action in scenario.ground_truth_actions
    if correct:
        metrics.correct_action_count += 1

    outcome_status = outcome.get("outcome", "FAILURE")
    if outcome_status == "SUCCESS":
        metrics.success_count += 1
        metrics.total_recovered_amount += outcome.get("recovered_amount", 0)

    # Memory metrics
    if eval_metrics.get("memory_contribution", "NONE") != "NONE":
        metrics.memory_contribution_count += 1

    metrics.evidence_accepted_total += eval_metrics.get("evidence_accepted", 0)
    metrics.evidence_discarded_total += eval_metrics.get("evidence_discarded", 0)

    # Stale evidence tracking
    if scenario.has_stale_memory:
        metrics.stale_evidence_detected += 1
        # If discarded evidence > 0, we rejected something
        if eval_metrics.get("evidence_discarded", 0) > 0:
            metrics.stale_evidence_correctly_rejected += 1

    # Category tracking
    cat = scenario.category
    if cat not in metrics.category_results:
        metrics.category_results[cat] = {"total": 0, "correct": 0, "success": 0}
    metrics.category_results[cat]["total"] += 1
    if correct:
        metrics.category_results[cat]["correct"] += 1
    if outcome_status == "SUCCESS":
        metrics.category_results[cat]["success"] += 1


def _outcome_for_decision(scenario: EvalScenario, action: str, decision: Any) -> str:
    """Resolve parameter-sensitive synthetic ground truth consistently for all systems."""
    parameter_key = action
    retry_after = getattr(decision, "retry_after_seconds", None)
    method = getattr(decision, "recommended_method", None)
    if isinstance(decision, dict):
        retry_after = decision.get("retry_after_seconds")
        method = decision.get("recommended_method")
    if action == "RETRY_AFTER" and retry_after is not None:
        parameter_key = f"{action}:{retry_after}"
    elif action == "SUGGEST_METHOD" and method:
        parameter_key = f"{action}:{method}"
    value = scenario.action_outcomes.get(parameter_key, scenario.action_outcomes.get(action, "FAILURE"))
    if isinstance(value, dict):
        value = value.get("outcome", value.get("status", "FAILURE"))
    return str(value)
