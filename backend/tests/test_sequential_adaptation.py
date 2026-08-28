"""Protocol and isolation tests for the sealed sequential evaluator."""
from __future__ import annotations

import json

from app.evaluation.sequential import (
    H1_SUPPORT_RULE,
    SEQUENTIAL_EVAL_SEEDS,
    environment_fingerprint,
    generate_sealed_environment,
    run_sequential_evaluation,
)


def test_sealed_environment_is_reproducible_and_seed_sensitive():
    first = generate_sealed_environment(11)
    repeated = generate_sealed_environment(11)
    different = generate_sealed_environment(29)

    assert environment_fingerprint(first) == environment_fingerprint(repeated)
    assert environment_fingerprint(first) != environment_fingerprint(different)
    assert SEQUENTIAL_EVAL_SEEDS == (11, 29, 47, 71, 101)


def test_static_and_adaptive_share_environment_without_hidden_context():
    result = run_sequential_evaluation(seeds=(11,), merchant_count=2, cases_per_merchant=12)
    seed_result = result["seed_results"][0]
    expected_fingerprint = environment_fingerprint(
        generate_sealed_environment(11, merchant_count=2, cases_per_merchant=12)
    )

    assert seed_result["environment_fingerprint"] == expected_fingerprint
    assert "probabilities" not in json.dumps(seed_result["conditions"])
    assert result["protocol"]["support_rule"] == H1_SUPPORT_RULE

    for condition in ("static", "adaptive"):
        condition_result = seed_result["conditions"][condition]
        assert [item["first_case_max_effective_n"] for item in condition_result["merchant_resets"]] == [0.0, 0.0]
        assert all(row["attempt_count_for_current_failure"] == 0 for row in condition_result["cases"])
        assert all(row["regret_rupees"] >= 0 for row in condition_result["cases"])

    adaptive_cases = seed_result["conditions"]["adaptive"]["cases"]
    assert max(row["max_effective_n_before_decision"] for row in adaptive_cases) > 0


def test_oracle_action_is_always_viable_and_regret_uses_only_viable_actions():
    environments = generate_sealed_environment(47, merchant_count=1, cases_per_merchant=10)
    result = run_sequential_evaluation(seeds=(47,), merchant_count=1, cases_per_merchant=10)
    cases_by_index = {case.index: case for case in environments[0].cases}

    for condition in ("static", "adaptive"):
        for row in result["seed_results"][0]["conditions"][condition]["cases"]:
            case = cases_by_index[row["case_index"]]
            assert row["optimal_viable_action"] in {action.value for action in case.allowed_actions}
            expected_optimal_probability = max(
                environments[0].probabilities[action] for action in case.allowed_actions
            )
            chosen_probability = environments[0].probabilities[
                next(action for action in case.allowed_actions if action.value == row["action"])
            ]
            expected_regret = max(0.0, expected_optimal_probability - chosen_probability) * case.amount / 100
            assert row["regret_rupees"] == round(expected_regret, 2)


def test_same_seed_reproduces_sequential_decisions_and_metrics():
    first = run_sequential_evaluation(seeds=(101,), merchant_count=1, cases_per_merchant=10)
    repeated = run_sequential_evaluation(seeds=(101,), merchant_count=1, cases_per_merchant=10)

    assert first["aggregate"] == repeated["aggregate"]
    assert first["seed_results"][0]["environment_fingerprint"] == repeated["seed_results"][0]["environment_fingerprint"]
    for condition in ("static", "adaptive"):
        first_cases = first["seed_results"][0]["conditions"][condition]["cases"]
        repeated_cases = repeated["seed_results"][0]["conditions"][condition]["cases"]
        stable_fields = ("case_index", "action", "outcome", "optimal_viable_action", "regret_rupees")
        assert [tuple(row[field] for field in stable_fields) for row in first_cases] == [
            tuple(row[field] for field in stable_fields) for row in repeated_cases
        ]
