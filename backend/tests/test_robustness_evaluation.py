from app.evaluation.robustness import run_robustness_evaluation


def test_robustness_suite_is_separate_zero_groq_and_policy_aware():
    result = run_robustness_evaluation(seeds=(11,), scenarios_per_seed=9)
    system = result["systems"]["system_c"]

    assert result["evaluation"] == "Robustness Evaluation"
    assert result["groq_calls"] == 0
    assert result["scenario_count"] == 9
    assert "blocked_payment_method" in system["coverage_by_scenario_type"]
    assert "merchant_policy_change" in system["coverage_by_scenario_type"]
    assert system["policy_violation_rate"] == 0
