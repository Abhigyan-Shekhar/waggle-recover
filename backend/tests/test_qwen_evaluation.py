from __future__ import annotations

import json

import pytest

from app.evaluation.qwen import run_qwen_evaluation


class _SafeQwen:
    def complete(self, **_: object) -> str:
        return json.dumps({
            "action": "SUGGEST_METHOD",
            "retry_after_seconds": None,
            "recommended_method": "upi",
            "confidence": 0.8,
            "reason": "Use a bounded alternative method.",
            "evidence_ids": [],
        })


class _InvalidQwen:
    def complete(self, **_: object) -> str:
        return "not-json"


def test_qwen_evaluation_is_separate_and_cacheable(tmp_path):
    cache = tmp_path / "qwen.json"
    result = run_qwen_evaluation(
        seed=31415,
        scenario_count=1,
        model="test-qwen",
        model_client=_SafeQwen(),
        cache_path=cache,
    ).to_dict()

    assert result["evaluation"] == "Qwen Recovery Evaluation"
    assert result["mode"] == "live_agent"
    assert result["valid_structured_output_rate"] == 1.0
    assert result["candidate_action_accuracy"] == 1.0
    assert result["final_post_policy_action_accuracy"] == 1.0
    assert cache.exists()
    assert "system_prompt" not in cache.read_text()


def test_qwen_evaluation_refuses_to_mislabel_fallback_as_live_model():
    with pytest.raises(ValueError, match="runtime Groq API key"):
        run_qwen_evaluation(scenario_count=1)


def test_deterministic_fallback_does_not_inflate_qwen_candidate_accuracy():
    result = run_qwen_evaluation(
        scenario_count=1,
        model="test-qwen",
        model_client=_InvalidQwen(),
    ).to_dict()

    assert result["valid_structured_output_rate"] == 0
    assert result["candidate_action_accuracy"] == 0
    assert result["fallback_rate"] == 1
