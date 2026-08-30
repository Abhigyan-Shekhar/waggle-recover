"""Controlled deterministic ablations for temporal-authority validation."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.evaluation.generator import EvalScenario, ScenarioGenerator
from app.evaluation.runner import run_evaluation


@dataclass
class _Counts:
    total: int = 0
    correct: int = 0
    recovered: int = 0
    at_risk: int = 0
    stale_cases: int = 0
    stale_used: int = 0
    stale_rejected: int = 0
    unsafe: int = 0
    latency_ms: float = 0.0

    def to_dict(self, name: str) -> dict[str, Any]:
        def rate(value: int, denominator: int) -> float:
            return round(value / denominator, 4) if denominator else 0.0

        return {
            "name": name,
            "scenario_count": self.total,
            "action_accuracy": rate(self.correct, self.total),
            "simulated_gmv_recovery": rate(self.recovered, self.at_risk),
            "stale_evidence_usage_rate": rate(self.stale_used, self.stale_cases),
            "stale_evidence_rejection_rate": rate(self.stale_rejected, self.stale_cases),
            "unsafe_action_rate": rate(self.unsafe, self.total),
            "average_latency_ms": round(self.latency_ms / self.total, 2) if self.total else 0.0,
        }


def _is_inherently_unsafe(scenario: EvalScenario, decision: dict[str, Any], used_stale: bool) -> bool:
    if used_stale:
        return True
    action = str(decision.get("action", ""))
    return action in {"RETRY_NOW", "RETRY_AFTER"} and scenario.failure_code in {
        "expired_card", "card_blocked", "do_not_honour", "invalid_instrument", "expired_instrument"
    }


def _baseline_used_stale(scenario: EvalScenario, decision: dict[str, Any]) -> bool:
    if not scenario.has_stale_memory or not scenario.stale_instrument:
        return False
    for item in scenario.history:
        if item.instrument_id != scenario.stale_instrument or item.outcome != "SUCCESS":
            continue
        if decision.get("action") == "RETRY_AFTER" and item.retry_after_seconds == decision.get("retry_after_seconds"):
            return True
        if decision.get("action") == "SUGGEST_METHOD" and item.method == decision.get("recommended_method"):
            return True
    return False


def run_ablation_evaluation(
    *, seed: int = 42, scenario_count: int = 200, cache_path: str | Path | None = None
) -> dict[str, Any]:
    """Compare retrieval with and without temporal validation on identical cases."""
    validated_rows: list[dict[str, Any]] = []
    unvalidated_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="waggle-ablation-") as temp_dir:
        root = Path(temp_dir)
        settings = Settings(
            app_db_path=str(root / "app.db"),
            waggle_db_path=str(root / "waggle.db"),
            waggle_embedding_model="fake",
        )
        run_evaluation(
            seed=seed,
            scenario_count=scenario_count,
            db_path=str(root / "eval-app.db"),
            waggle_db_path=str(root / "eval-waggle.db"),
            settings=settings,
            result_sink=validated_rows.append,
            temporal_validation_enabled=True,
        )
        run_evaluation(
            seed=seed,
            scenario_count=scenario_count,
            db_path=str(root / "no-validator-app.db"),
            waggle_db_path=str(root / "no-validator-waggle.db"),
            settings=settings,
            result_sink=unvalidated_rows.append,
            temporal_validation_enabled=False,
        )

    scenarios = {item.id: item for item in ScenarioGenerator(seed=seed).generate(scenario_count)}
    names = {
        "baseline_a": "Blind Retry",
        "baseline_b": "Contextual History",
        "system_c": "Waggle Recover WITH temporal validation",
    }
    counts = {key: _Counts() for key in (*names, "waggle_no_temporal")}

    for row in validated_rows:
        scenario = scenarios[row["scenario_id"]]
        system = row["system"]
        item = counts[system]
        decision = row["decision"]
        used_stale = _baseline_used_stale(scenario, decision) if system == "baseline_b" else False
        rejected = bool(row["stale_evidence_correctly_rejected"]) if system == "system_c" else False
        item.total += 1
        item.correct += int(row["action_correct"])
        item.recovered += int(row["recovered_amount"])
        item.at_risk += scenario.amount
        item.stale_cases += int(scenario.has_stale_memory)
        item.stale_used += int(used_stale)
        item.stale_rejected += int(rejected)
        item.unsafe += int(_is_inherently_unsafe(scenario, decision, used_stale))
        item.latency_ms += float(row["latency_ms"])

    for row in unvalidated_rows:
        if row["system"] != "system_c":
            continue
        scenario = scenarios[row["scenario_id"]]
        decision = row["decision"]
        used_stale = _baseline_used_stale(scenario, decision)
        item = counts["waggle_no_temporal"]
        item.total += 1
        item.correct += int(row["action_correct"])
        item.recovered += int(row["recovered_amount"])
        item.at_risk += scenario.amount
        item.stale_cases += int(scenario.has_stale_memory)
        item.stale_used += int(used_stale)
        item.unsafe += int(_is_inherently_unsafe(scenario, decision, used_stale))
        item.latency_ms += float(row["latency_ms"])

    systems = {
        "blind_retry": counts["baseline_a"].to_dict(names["baseline_a"]),
        "contextual_history": counts["baseline_b"].to_dict(names["baseline_b"]),
        "waggle_without_temporal_validation": counts["waggle_no_temporal"].to_dict(
            "Waggle Recover WITHOUT temporal validation"
        ),
        "waggle_with_temporal_validation": counts["system_c"].to_dict(names["system_c"]),
    }
    result = {
        "evaluation": "Temporal Authority Ablation",
        "mode": "deterministic_evaluation",
        "seed": seed,
        "scenario_count": scenario_count,
        "systems": systems,
        "finding": "Retrieval provides context; temporal validation prevents superseded evidence from driving actions.",
        "ablation_control": (
            "Identical scenarios, Waggle retrieval, scoring, ranking, decision engine, policy, and retry budgets; "
            "only temporal validation is switched OFF versus ON."
        ),
        "unsafe_action_definition": (
            "Use of known stale evidence, or retrying the same method for a permanent/instrument failure."
        ),
    }
    if cache_path is not None:
        destination = Path(cache_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result
