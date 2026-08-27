"""Tests for the evaluation harness and baseline systems."""
from __future__ import annotations

import pytest

from app.domain.enums import RecoveryAction
from app.evaluation.baselines import BlindFixedRetryBaseline, ContextualHistoryBaseline
from app.evaluation.generator import ScenarioGenerator
from app.evaluation.metrics import ComparisonSummary, SystemMetrics
from app.evaluation.runner import (
    _decision_is_correct,
    _emit_result,
    _outcome_for_decision,
    _stale_evidence_correctly_rejected,
)


@pytest.fixture
def generator():
    return ScenarioGenerator(seed=42)


class TestScenarioGenerator:
    def test_generates_requested_count(self, generator):
        scenarios = generator.generate(50)
        assert len(scenarios) == 50

    def test_all_scenarios_have_required_fields(self, generator):
        scenarios = generator.generate(20)
        for s in scenarios:
            assert s.customer_id
            assert s.merchant_id
            assert s.failure_code
            assert s.ground_truth_actions
            assert len(s.action_outcomes) > 0

    def test_curated_scenarios_are_deterministic(self, generator):
        s1 = generator._curated_scenarios()
        gen2 = ScenarioGenerator(seed=42)
        s2 = gen2._curated_scenarios()
        assert len(s1) == len(s2)
        assert s1[0].customer_id == s2[0].customer_id
        assert s1[0].name == s2[0].name

    def test_stale_card_trap_has_stale_memory(self, generator):
        scenarios = generator._curated_scenarios()
        stale_trap = next((s for s in scenarios if s.name == "Stale Card Trap"), None)
        assert stale_trap is not None
        assert stale_trap.has_stale_memory is True
        assert stale_trap.stale_instrument == "card_1234"
        assert stale_trap.current_instrument == "card_9988"

    def test_no_history_scenario_has_empty_history(self, generator):
        scenarios = generator._curated_scenarios()
        no_hist = next((s for s in scenarios if s.name == "No Memory Control"), None)
        assert no_hist is not None
        assert no_hist.history == []
        assert not no_hist.has_useful_memory

    def test_different_seeds_produce_different_scenarios(self):
        gen1 = ScenarioGenerator(seed=1)
        gen2 = ScenarioGenerator(seed=999)
        s1 = gen1.generate(30)
        s2 = gen2.generate(30)
        # At least one scenario should differ
        assert any(
            s1[i].customer_id != s2[i].customer_id or s1[i].failure_code != s2[i].failure_code
            for i in range(min(len(s1), len(s2)))
        )

    def test_full_200_case_generator_uses_parameter_ground_truth(self, generator):
        scenarios = generator.generate(200)
        assert len(scenarios) == 200
        for scenario in scenarios:
            if "RETRY_AFTER" in scenario.action_outcomes:
                assert scenario.action_outcomes["RETRY_AFTER"] == "FAILURE"
            if "SUGGEST_METHOD" in scenario.action_outcomes:
                assert scenario.action_outcomes["SUGGEST_METHOD"] == "FAILURE"

        assert any(key.startswith("RETRY_AFTER:") for scenario in scenarios for key in scenario.action_outcomes)
        assert any(key.startswith("SUGGEST_METHOD:") for scenario in scenarios for key in scenario.action_outcomes)

        failed_alternatives = [scenario for scenario in scenarios if scenario.category == "failed_alternative"]
        assert failed_alternatives
        assert all(
            len([event for event in scenario.history if event.action_taken == "SUGGEST_METHOD"]) >= 2
            for scenario in failed_alternatives
        )


class TestBaselineA:
    def test_always_retries_on_first_attempt(self, generator):
        baseline = BlindFixedRetryBaseline()
        scenarios = generator.generate(10)
        for s in scenarios:
            decision = baseline.decide(s, retry_count=0)
            # Baseline A should always retry (except no scenario has 0 attempts as stop)
            assert decision.action in (RecoveryAction.RETRY_AFTER, RecoveryAction.STOP)

    def test_stops_at_max_attempts(self, generator):
        baseline = BlindFixedRetryBaseline()
        scenarios = generator.generate(5)
        for s in scenarios:
            decision = baseline.decide(s, retry_count=3)
            assert decision.action == RecoveryAction.STOP

    def test_no_memory_contribution(self, generator):
        from app.domain.enums import MemoryContribution
        baseline = BlindFixedRetryBaseline()
        scenarios = generator.generate(5)
        for s in scenarios:
            decision = baseline.decide(s)
            assert decision.memory_contribution == MemoryContribution.NONE


class TestBaselineB:
    def test_uses_history_for_timing(self, generator):
        baseline = ContextualHistoryBaseline()
        # timing_memory scenario should produce RETRY_AFTER
        scenarios = generator._curated_scenarios()
        timing = next((s for s in scenarios if s.name == "Timing Memory"), None)
        assert timing is not None

        decision = baseline.decide(timing)
        assert decision.action in (RecoveryAction.RETRY_AFTER, RecoveryAction.SUGGEST_METHOD)

    def test_permanent_failure_suggests_method(self, generator):
        baseline = ContextualHistoryBaseline()
        scenarios = generator.generate(20)
        permanent = [s for s in scenarios if s.failure_code in ("expired_card", "card_blocked")]

        for s in permanent[:3]:
            decision = baseline.decide(s)
            assert decision.action == RecoveryAction.SUGGEST_METHOD

    def test_no_history_falls_back_to_transient_retry(self, generator):
        baseline = ContextualHistoryBaseline()
        scenarios = generator._curated_scenarios()
        no_hist = next((s for s in scenarios if s.name == "No Memory Control"), None)
        assert no_hist is not None

        decision = baseline.decide(no_hist)
        assert decision.action in (RecoveryAction.RETRY_AFTER, RecoveryAction.CUSTOMER_NUDGE)


class TestMetrics:
    def test_action_accuracy_calculation(self):
        m = SystemMetrics(name="test")
        m.scenario_count = 10
        m.correct_action_count = 7
        assert abs(m.action_accuracy - 0.7) < 0.001

    def test_zero_scenarios_no_divide_by_zero(self):
        m = SystemMetrics(name="test")
        assert m.action_accuracy == 0.0
        assert m.success_rate == 0.0
        assert m.recovery_rate_gmv == 0.0

    def test_comparison_summary_to_dict(self):
        a = SystemMetrics(name="A", scenario_count=10, correct_action_count=5)
        b = SystemMetrics(name="B", scenario_count=10, correct_action_count=7)
        c = SystemMetrics(name="C", scenario_count=10, correct_action_count=9)
        summary = ComparisonSummary(baseline_a=a, baseline_b=b, system_c=c, scenario_count=10)
        d = summary.to_dict()
        assert "systems" in d
        assert "improvements" in d
        assert d["improvements"]["c_vs_a_accuracy"] > 0
        assert d["improvements"]["c_vs_b_accuracy"] > 0


class TestEvaluationEvidence:
    def test_retry_outcome_requires_matching_timing_parameter(self, generator):
        scenario = generator._curated_scenarios()[0]
        scenario.action_outcomes = {"RETRY_AFTER": "SUCCESS", "RETRY_AFTER:60": "FAILURE"}

        outcome = _outcome_for_decision(
            scenario,
            "RETRY_AFTER",
            {"action": "RETRY_AFTER", "retry_after_seconds": 60},
        )

        assert outcome == "FAILURE"

    def test_result_sink_records_auditable_stale_rejection(self, generator):
        scenario = next(item for item in generator._curated_scenarios() if item.name == "Stale Card Trap")
        rows = []

        _emit_result(
            rows.append,
            scenario,
            "system_c",
            {"action": "SUGGEST_METHOD", "recommended_method": "upi"},
            "SUCCESS",
            4.2,
            {
                "memory_contribution": "DECISIVE",
                "retrieval_mode": "FULL_CONTEXT",
                "evidence_accepted": 1,
                "evidence_discarded": 2,
                "accepted_instruments": ["card_9988"],
                "discarded_instruments": ["card_1234"],
            },
            recovered_amount=scenario.amount,
        )

        assert rows[0]["stale_evidence_detected"] is True
        assert rows[0]["stale_evidence_correctly_rejected"] is True
        assert rows[0]["discarded_count"] == 2
        assert rows[0]["decision"]["recommended_method"] == "upi"
        assert rows[0]["decision"]["outcome"] == "SUCCESS"

    def test_unrelated_discard_does_not_inflate_stale_metric(self, generator):
        scenario = next(item for item in generator._curated_scenarios() if item.name == "Stale Card Trap")
        assert not _stale_evidence_correctly_rejected(
            scenario,
            {"discarded_instruments": ["card_unrelated"], "accepted_instruments": []},
        )

    def test_correct_action_with_wrong_parameter_is_incorrect(self, generator):
        scenario = next(item for item in generator._curated_scenarios() if item.name == "Timing Memory")
        decision = {"action": "RETRY_AFTER", "retry_after_seconds": 999}
        assert _outcome_for_decision(scenario, "RETRY_AFTER", decision) == "FAILURE"
        assert not _decision_is_correct(scenario, "RETRY_AFTER", decision)
