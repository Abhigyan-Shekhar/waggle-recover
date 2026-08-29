from app.evaluation.ablations import run_ablation_evaluation


def test_temporal_ablation_exposes_stale_usage_difference():
    result = run_ablation_evaluation(seed=42, scenario_count=6)
    without = result["systems"]["waggle_without_temporal_validation"]
    with_validation = result["systems"]["waggle_with_temporal_validation"]

    assert without["stale_evidence_usage_rate"] > 0
    assert without["stale_evidence_rejection_rate"] == 0
    assert with_validation["stale_evidence_usage_rate"] == 0
    assert with_validation["stale_evidence_rejection_rate"] == 1.0
