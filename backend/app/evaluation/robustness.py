"""Multi-seed 1,000+ case deterministic robustness evaluation."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import Settings
from app.evaluation.ablations import _baseline_used_stale, _is_inherently_unsafe
from app.evaluation.generator import EvalScenario, ScenarioGenerator, ScenarioHistory
from app.evaluation.runner import run_evaluation

DEFAULT_ROBUSTNESS_SEEDS = (11, 29, 47, 83, 131)


def _with_robustness_cases(seed: int, count: int) -> list[EvalScenario]:
    scenarios = ScenarioGenerator(seed=seed).generate(count)
    if len(scenarios) < 9:
        return scenarios
    now = datetime.now(UTC)

    blocked = scenarios[6]
    scenarios[6] = replace(
        blocked,
        name="Blocked Payment Method",
        category="blocked_payment_method",
        failure_code="issuer_unavailable",
        failure_reason="Current method is disabled by merchant policy",
        history=[],
        ground_truth_actions=["SUGGEST_METHOD"],
        action_outcomes={
            "RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE",
            "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS",
            "STOP": "SKIPPED", "ESCALATE": "SKIPPED",
        },
        merchant_policies=[{
            "policy_id": f"policy-blocked-{seed}",
            "version": 1,
            "allowed_actions": ["SUGGEST_METHOD", "STOP"],
            "blocked_methods": [blocked.method],
        }],
    )

    changed = scenarios[7]
    old_policy_id = f"policy-old-{seed}"
    scenarios[7] = replace(
        changed,
        name="Policy Changed",
        category="merchant_policy_change",
        method="card",
        instrument_id=f"card_policy_{seed}",
        failure_code="issuer_unavailable",
        failure_reason="Issuer unavailable under a superseding merchant policy",
        history=[
            ScenarioHistory(
                event_type="failure", payment_id=f"pay_policy_hist_{seed}",
                customer_id=changed.customer_id, merchant_id=changed.merchant_id,
                amount=changed.amount, method="card", instrument_id=f"card_policy_{seed}",
                failure_code="issuer_unavailable", outcome="FAILURE",
                timestamp=now - timedelta(days=10),
            ),
            ScenarioHistory(
                event_type="success", payment_id=f"pay_policy_success_{seed}",
                customer_id=changed.customer_id, merchant_id=changed.merchant_id,
                amount=changed.amount, method="card", instrument_id=f"card_policy_{seed}",
                outcome="SUCCESS", action_taken="RETRY_AFTER", retry_after_seconds=300,
                timestamp=now - timedelta(days=10, minutes=-5),
            ),
        ],
        instruments=[{"alias": f"card_policy_{seed}", "type": "card", "status": "active"}],
        ground_truth_actions=["SUGGEST_METHOD"],
        action_outcomes={
            "RETRY_AFTER": "FAILURE", "RETRY_AFTER:300": "FAILURE",
            "RETRY_AFTER:900": "FAILURE", "SUGGEST_METHOD": "SUCCESS",
            "SUGGEST_METHOD:upi": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS",
            "STOP": "SKIPPED", "ESCALATE": "SKIPPED",
        },
        merchant_policies=[
            {
                "policy_id": old_policy_id,
                "version": 1,
                "effective_from": now - timedelta(days=30),
                "min_retry_interval_seconds": 300,
                "allowed_actions": ["RETRY_AFTER", "STOP"],
            },
            {
                "policy_id": f"policy-new-{seed}",
                "version": 2,
                "effective_from": now - timedelta(days=1),
                "supersedes_policy_id": old_policy_id,
                "min_retry_interval_seconds": 900,
                "allowed_actions": ["SUGGEST_METHOD", "STOP"],
                "blocked_methods": ["card"],
            },
        ],
    )

    contradictory = scenarios[8]
    scenarios[8] = replace(
        contradictory,
        name="Contradictory Evidence",
        category="contradictory_evidence",
    )
    return scenarios


@dataclass
class _Aggregate:
    total: int = 0
    correct: int = 0
    successes: int = 0
    recovered: int = 0
    at_risk: int = 0
    stale_cases: int = 0
    stale_rejected: int = 0
    unsafe: int = 0
    unnecessary_escalations: int = 0
    policy_violations: int = 0
    categories: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, scenario: EvalScenario, row: dict[str, Any]) -> None:
        decision = row["decision"]
        action = str(decision.get("action", ""))
        used_stale = _baseline_used_stale(scenario, decision)
        self.total += 1
        self.correct += int(row["action_correct"])
        self.successes += int(row["outcome"] == "SUCCESS")
        self.recovered += int(row["recovered_amount"])
        self.at_risk += scenario.amount
        self.stale_cases += int(scenario.has_stale_memory)
        self.stale_rejected += int(row["stale_evidence_correctly_rejected"])
        self.unsafe += int(_is_inherently_unsafe(scenario, decision, used_stale))
        self.unnecessary_escalations += int(
            action == "ESCALATE" and "ESCALATE" not in scenario.ground_truth_actions
        )
        if scenario.merchant_policies:
            current = scenario.merchant_policies[-1]
            allowed = set(current.get("allowed_actions", []))
            blocked = set(current.get("blocked_methods", []))
            method = decision.get("recommended_method")
            self.policy_violations += int(
                (bool(allowed) and action not in allowed and action != "ESCALATE")
                or (bool(method) and method in blocked)
            )
        category = self.categories.setdefault(scenario.category, {"total": 0, "correct": 0, "unsafe": 0})
        category["total"] += 1
        category["correct"] += int(row["action_correct"])
        category["unsafe"] += int(_is_inherently_unsafe(scenario, decision, used_stale))

    def to_dict(self) -> dict[str, Any]:
        def rate(value: int, denominator: int) -> float:
            return round(value / denominator, 4) if denominator else 0.0

        coverage = {
            name: {
                **values,
                "action_accuracy": rate(values["correct"], values["total"]),
                "unsafe_action_rate": rate(values["unsafe"], values["total"]),
            }
            for name, values in sorted(self.categories.items())
        }
        return {
            "scenario_count": self.total,
            "parameter_aware_action_accuracy": rate(self.correct, self.total),
            "recovery_success": rate(self.successes, self.total),
            "simulated_gmv_recovery": rate(self.recovered, self.at_risk),
            "stale_rejection": rate(self.stale_rejected, self.stale_cases),
            "unsafe_action_rate": rate(self.unsafe, self.total),
            "unnecessary_escalation_rate": rate(self.unnecessary_escalations, self.total),
            "policy_violation_rate": rate(self.policy_violations, self.total),
            "coverage_by_scenario_type": coverage,
        }


def run_robustness_evaluation(
    *,
    seeds: tuple[int, ...] = DEFAULT_ROBUSTNESS_SEEDS,
    scenarios_per_seed: int = 200,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run deterministic isolated worlds across fixed seeds (zero Groq calls)."""
    if not seeds or scenarios_per_seed < 1:
        raise ValueError("At least one seed and one scenario per seed are required")
    aggregates = {key: _Aggregate() for key in ("baseline_a", "baseline_b", "system_c")}

    with tempfile.TemporaryDirectory(prefix="waggle-robustness-") as temp_dir:
        root = Path(temp_dir)
        settings = Settings(
            app_db_path=str(root / "app.db"),
            waggle_db_path=str(root / "waggle.db"),
            waggle_embedding_model="fake",
            decision_provider="deterministic",
            groq_api_key="",
        )
        for seed in seeds:
            scenarios = _with_robustness_cases(seed, scenarios_per_seed)
            rows: list[dict[str, Any]] = []
            run_evaluation(
                seed=seed,
                scenario_count=len(scenarios),
                db_path=str(root / f"app-{seed}.db"),
                waggle_db_path=str(root / f"waggle-{seed}.db"),
                settings=settings,
                result_sink=rows.append,
                scenarios=scenarios,
            )
            by_id = {item.id: item for item in scenarios}
            for row in rows:
                aggregates[row["system"]].add(by_id[row["scenario_id"]], row)

    result = {
        "evaluation": "Robustness Evaluation",
        "mode": "deterministic_evaluation",
        "groq_calls": 0,
        "seeds": list(seeds),
        "scenario_count": len(seeds) * scenarios_per_seed,
        "systems": {key: aggregate.to_dict() for key, aggregate in aggregates.items()},
        "disclosure": "All money and outcomes are seeded simulations; this is not production GMV.",
    }
    if cache_path is not None:
        destination = Path(cache_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result
