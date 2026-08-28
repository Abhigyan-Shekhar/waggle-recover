"""Sealed sequential evaluation for evidence-weighted strategy adaptation.

The hidden environment and constants in this module are pre-registered. They
must not be changed after looking at evaluation results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph

from app.config import Settings
from app.domain.enums import OutcomeStatus, RecoveryAction
from app.domain.models import MerchantPolicy, NormalizedPaymentEvent, PaymentInstrument
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.decision_engine import DeterministicDecisionProvider
from app.recovery.orchestrator import RecoveryOrchestrator

SEQUENTIAL_EVAL_SEEDS = (11, 29, 47, 71, 101)
MERCHANT_COUNT = 3
CASES_PER_MERCHANT = 30
PHASES = {"cold": (1, 10), "intermediate": (11, 20), "warm": (21, 30)}
HYPOTHESIS_H1 = (
    "With identical safe action sets and hidden potential outcomes, online evidence-weighted "
    "strategy adaptation improves mean recovery success and reduces cumulative viable-action "
    "regret versus a static deterministic policy."
)
H1_SUPPORT_RULE = "adaptive mean success_rate > static and adaptive mean cumulative_regret < static"
STRATEGIES = (
    RecoveryAction.RETRY_AFTER,
    RecoveryAction.SUGGEST_METHOD,
    RecoveryAction.CUSTOMER_NUDGE,
)


@dataclass(frozen=True)
class SequentialCase:
    index: int
    amount: int
    allowed_actions: tuple[RecoveryAction, ...]
    potential_outcomes: dict[RecoveryAction, bool]


@dataclass(frozen=True)
class MerchantEnvironment:
    merchant_id: str
    probabilities: dict[RecoveryAction, float]
    cases: tuple[SequentialCase, ...]


def generate_sealed_environment(
    seed: int,
    *,
    merchant_count: int = MERCHANT_COUNT,
    cases_per_merchant: int = CASES_PER_MERCHANT,
) -> tuple[MerchantEnvironment, ...]:
    """Generate all hidden probabilities and potential outcomes before either condition runs."""
    rng = random.Random(seed)
    environments: list[MerchantEnvironment] = []
    for merchant_index in range(merchant_count):
        preferred = STRATEGIES[merchant_index % len(STRATEGIES)]
        probabilities = {
            action: round(rng.uniform(0.72, 0.86), 4)
            if action == preferred
            else round(rng.uniform(0.25, 0.50), 4)
            for action in STRATEGIES
        }
        cases: list[SequentialCase] = []
        forced_cycle = (
            RecoveryAction.RETRY_AFTER,
            RecoveryAction.SUGGEST_METHOD,
            RecoveryAction.CUSTOMER_NUDGE,
            RecoveryAction.RETRY_AFTER,
            RecoveryAction.SUGGEST_METHOD,
            RecoveryAction.CUSTOMER_NUDGE,
        )
        for index in range(1, cases_per_merchant + 1):
            amount = rng.choice((100_000, 250_000, 500_000, 800_000, 1_200_000))
            phase_position = (index - 1) % 10
            allowed = (
                (forced_cycle[phase_position],)
                if phase_position < len(forced_cycle)
                else STRATEGIES
            )
            draws = {action: rng.random() for action in STRATEGIES}
            cases.append(SequentialCase(
                index=index,
                amount=amount,
                allowed_actions=allowed,
                potential_outcomes={
                    action: draws[action] < probabilities[action] for action in STRATEGIES
                },
            ))
        environments.append(MerchantEnvironment(
            merchant_id=f"SEQ-MERCHANT-{merchant_index + 1:02d}",
            probabilities=probabilities,
            cases=tuple(cases),
        ))
    return tuple(environments)


def environment_fingerprint(environments: tuple[MerchantEnvironment, ...]) -> str:
    payload = [
        {
            "merchant_id": env.merchant_id,
            "probabilities": {action.value: value for action, value in env.probabilities.items()},
            "cases": [
                {
                    "index": case.index,
                    "amount": case.amount,
                    "allowed": [action.value for action in case.allowed_actions],
                    "outcomes": {action.value: value for action, value in case.potential_outcomes.items()},
                }
                for case in env.cases
            ],
        }
        for env in environments
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_sequential_evaluation(
    *,
    seeds: tuple[int, ...] = SEQUENTIAL_EVAL_SEEDS,
    merchant_count: int = MERCHANT_COUNT,
    cases_per_merchant: int = CASES_PER_MERCHANT,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run paired static/adaptive streams against sealed environments."""
    active_settings = settings or Settings()
    seed_results = []
    for seed in seeds:
        environment = generate_sealed_environment(
            seed,
            merchant_count=merchant_count,
            cases_per_merchant=cases_per_merchant,
        )
        fingerprint = environment_fingerprint(environment)
        conditions = {
            condition: _run_condition(environment, condition, active_settings)
            for condition in ("static", "adaptive")
        }
        seed_results.append({
            "seed": seed,
            "environment_fingerprint": fingerprint,
            "conditions": conditions,
        })

    aggregate = {
        condition: _aggregate_condition(seed_results, condition)
        for condition in ("static", "adaptive")
    }
    h1_supported = (
        aggregate["adaptive"]["overall"]["success_rate"]["mean"]
        > aggregate["static"]["overall"]["success_rate"]["mean"]
        and aggregate["adaptive"]["overall"]["cumulative_regret_rupees"]["mean"]
        < aggregate["static"]["overall"]["cumulative_regret_rupees"]["mean"]
    )
    return {
        "protocol": {
            "sealed_before_results": True,
            "seeds": list(seeds),
            "merchant_count": merchant_count,
            "cases_per_merchant": cases_per_merchant,
            "phases": PHASES,
            "controlled_exploration_per_10": [
                RecoveryAction.RETRY_AFTER.value,
                RecoveryAction.SUGGEST_METHOD.value,
                RecoveryAction.CUSTOMER_NUDGE.value,
                RecoveryAction.RETRY_AFTER.value,
                RecoveryAction.SUGGEST_METHOD.value,
                RecoveryAction.CUSTOMER_NUDGE.value,
            ],
            "decision_opportunities_per_10": 4,
            "overall_optimal_rate_includes_forced_exploration": True,
            "kappa": active_settings.strategy_prior_kappa,
            "min_effective_n": active_settings.strategy_min_effective_n,
            "half_life_days": active_settings.evidence_recency_half_life_days,
            "hypothesis_h1": HYPOTHESIS_H1,
            "support_rule": H1_SUPPORT_RULE,
        },
        "seed_results": seed_results,
        "aggregate": aggregate,
        "h1_supported": h1_supported,
    }


def _run_condition(
    environments: tuple[MerchantEnvironment, ...],
    condition: str,
    settings: Settings,
) -> dict[str, Any]:
    phase_rows: dict[str, list[dict[str, Any]]] = {phase: [] for phase in PHASES}
    all_rows: list[dict[str, Any]] = []
    merchant_resets: list[dict[str, Any]] = []
    embedding_model = EmbeddingModel(settings.waggle_embedding_model)
    with tempfile.TemporaryDirectory(prefix=f"waggle-sequential-{condition}-") as temp_dir:
        for env in environments:
            # Every merchant starts in a fresh world; memory persists only within its stream.
            db_path = str(Path(temp_dir) / f"{env.merchant_id}.sqlite")
            graph_path = str(Path(temp_dir) / f"{env.merchant_id}-waggle.sqlite")
            graph = MemoryGraph(
                db_path=graph_path,
                embedding_model=embedding_model,
            ).for_tenant(f"sequential-{condition}-{env.merchant_id}")
            adapter = WaggleRecoveryMemoryAdapter(graph)
            db = Database(db_path)
            provider = DeterministicDecisionProvider(enable_strategy_priors=condition == "adaptive")
            orchestrator = RecoveryOrchestrator(adapter, db, provider, settings)
            _register_active_card(adapter, db, env.merchant_id)

            first_prior_n: float | None = None
            for case in env.cases:
                policy = MerchantPolicy(
                    merchant_id=env.merchant_id,
                    allowed_actions=[*case.allowed_actions, RecoveryAction.STOP],
                )
                event = NormalizedPaymentEvent(
                    event_type="payment.failed",
                    payment_id=f"{condition}-{env.merchant_id}-{case.index:03d}",
                    customer_id=f"SEQ-CUSTOMER-{env.merchant_id}",
                    merchant_id=env.merchant_id,
                    amount=case.amount,
                    method="card",
                    instrument_id="card_active",
                    error_code="issuer_unavailable",
                    error_description="Issuer temporarily unavailable",
                    created_at=datetime.now(UTC) + timedelta(seconds=case.index),
                    source="sequential_evaluation",
                )
                outcomes = {
                    action.value: "SUCCESS" if success else "FAILURE"
                    for action, success in case.potential_outcomes.items()
                }
                result = orchestrator.process_event(
                    event,
                    merchant_policy=policy,
                    simulation_outcomes=outcomes,
                    decision_provider=provider,
                )
                action = RecoveryAction(result["decision"]["action"])
                viable = tuple(case.allowed_actions)
                optimal = max(viable, key=lambda candidate: env.probabilities[candidate])
                chosen_probability = env.probabilities.get(action, 0.0)
                regret_rupees = max(0.0, env.probabilities[optimal] - chosen_probability) * case.amount / 100
                priors = result["strategy_priors"]
                prior_n = max((float(item["effective_n"]) for item in priors), default=0.0)
                if first_prior_n is None:
                    first_prior_n = prior_n
                row = {
                    "merchant_id": env.merchant_id,
                    "case_index": case.index,
                    "phase": _phase_for(case.index),
                    "action": action.value,
                    "outcome": result["outcome"]["outcome"],
                    "amount": case.amount,
                    "recovered_amount": result["outcome"]["recovered_amount"],
                    "optimal_viable_action": optimal.value,
                    "optimal_viable_action_selected": action == optimal,
                    "decision_opportunity": len(viable) > 1,
                    "regret_rupees": round(regret_rupees, 2),
                    "max_effective_n_before_decision": prior_n,
                    "attempt_count_for_current_failure": result["metrics"]["attempt_count_for_current_failure"],
                }
                all_rows.append(row)
                phase_rows[row["phase"]].append(row)
            merchant_resets.append({
                "merchant_id": env.merchant_id,
                "first_case_max_effective_n": first_prior_n or 0.0,
            })

    return {
        "overall": _summarize_rows(all_rows),
        "phases": {phase: _summarize_rows(rows) for phase, rows in phase_rows.items()},
        "merchant_resets": merchant_resets,
        "cases": all_rows,
    }


def _register_active_card(
    adapter: WaggleRecoveryMemoryAdapter,
    db: Database,
    merchant_id: str,
) -> None:
    instrument = PaymentInstrument(
        customer_id=f"SEQ-CUSTOMER-{merchant_id}",
        instrument_type="card",
        fingerprint_or_safe_alias="card_active",
    )
    instrument.waggle_node_id = adapter.store_payment_instrument(instrument)
    db.upsert_instrument(instrument.model_dump(mode="json"))


def _phase_for(index: int) -> str:
    for phase, (start, end) in PHASES.items():
        if start <= index <= end:
            return phase
    return "warm"


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    successes = sum(row["outcome"] == OutcomeStatus.SUCCESS.value for row in rows)
    at_risk = sum(row["amount"] for row in rows)
    recovered = sum(row["recovered_amount"] for row in rows)
    optimal = sum(row["optimal_viable_action_selected"] for row in rows)
    opportunity_rows = [row for row in rows if row["decision_opportunity"]]
    opportunity_optimal = sum(row["optimal_viable_action_selected"] for row in opportunity_rows)
    return {
        "case_count": count,
        "success_rate": round(successes / count, 6) if count else 0.0,
        "recovered_gmv_rupees": round(recovered / 100, 2),
        "gmv_recovery_rate": round(recovered / at_risk, 6) if at_risk else 0.0,
        "optimal_viable_action_rate": round(optimal / count, 6) if count else 0.0,
        "decision_opportunity_count": len(opportunity_rows),
        "optimal_action_rate_on_decision_opportunities": (
            round(opportunity_optimal / len(opportunity_rows), 6) if opportunity_rows else 0.0
        ),
        "cumulative_regret_rupees": round(sum(row["regret_rupees"] for row in rows), 2),
    }


def _aggregate_condition(seed_results: list[dict[str, Any]], condition: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scope in ("overall", *PHASES):
        summaries = []
        for item in seed_results:
            condition_result = item["conditions"][condition]
            summaries.append(
                condition_result["overall"] if scope == "overall" else condition_result["phases"][scope]
            )
        result[scope] = {}
        for metric in (
            "success_rate",
            "recovered_gmv_rupees",
            "gmv_recovery_rate",
            "optimal_viable_action_rate",
            "optimal_action_rate_on_decision_opportunities",
            "cumulative_regret_rupees",
        ):
            values = [float(summary[metric]) for summary in summaries]
            result[scope][metric] = {
                "mean": round(statistics.fmean(values), 6),
                "std": round(statistics.pstdev(values), 6),
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sealed sequential adaptation evaluation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_sequential_evaluation()
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
