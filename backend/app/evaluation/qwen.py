"""Separate, cacheable Qwen evaluation for bounded recovery candidates.

This module deliberately does not share headline metrics with the deterministic
200-case benchmark. A failed or unavailable model call is reported as fallback;
it is never counted as a successful Qwen structured output.
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.domain.models import NormalizedPaymentEvent
from app.evaluation.generator import ScenarioGenerator
from app.evaluation.runner import (
    _decision_is_correct,
    _outcome_for_decision,
    _populate_memory,
)
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.agent import AgentDecisionProvider, AgentModelClient
from app.recovery.orchestrator import RecoveryOrchestrator


@dataclass
class QwenEvaluationSummary:
    seed: int
    scenario_count: int
    model: str
    structured_output_count: int = 0
    candidate_correct_count: int = 0
    final_correct_count: int = 0
    stale_scenario_count: int = 0
    stale_evidence_citation_count: int = 0
    forbidden_rejected_evidence_usage_count: int = 0
    policy_modification_count: int = 0
    policy_block_count: int = 0
    safe_escalation_count: int = 0
    fallback_count: int = 0
    hallucinated_evidence_count: int = 0
    model_latencies_ms: list[float] = field(default_factory=list)
    token_usage: int | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _rate(value: int, denominator: int) -> float:
        return round(value / denominator, 4) if denominator else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        total = self.scenario_count
        data.update({
            "evaluation": "Qwen Recovery Evaluation",
            "mode": "live_agent",
            "valid_structured_output_rate": self._rate(self.structured_output_count, total),
            "candidate_action_accuracy": self._rate(
                self.candidate_correct_count, self.structured_output_count
            ),
            "final_post_policy_action_accuracy": self._rate(self.final_correct_count, total),
            "stale_evidence_citation_rate": self._rate(
                self.stale_evidence_citation_count, self.stale_scenario_count
            ),
            "forbidden_rejected_evidence_usage_rate": self._rate(
                self.forbidden_rejected_evidence_usage_count, total
            ),
            "policy_modification_rate": self._rate(self.policy_modification_count, total),
            "policy_block_rate": self._rate(self.policy_block_count, total),
            "safe_escalation_rate": self._rate(self.safe_escalation_count, total),
            "fallback_rate": self._rate(self.fallback_count, total),
            "hallucinated_evidence_rate": self._rate(self.hallucinated_evidence_count, total),
            "average_model_latency_ms": round(
                sum(self.model_latencies_ms) / len(self.model_latencies_ms), 2
            ) if self.model_latencies_ms else 0.0,
            "disclosure": (
                "Candidate metrics measure Qwen before PolicyEngine. Final metrics measure the "
                "post-policy action. Cached rows contain concise structured outputs only."
            ),
        })
        return data


def run_qwen_evaluation(
    *,
    seed: int = 31415,
    scenario_count: int = 50,
    model: str = "qwen/qwen3.8-27b",
    api_key: str = "",
    model_client: AgentModelClient | None = None,
    cache_path: str | Path | None = None,
) -> QwenEvaluationSummary:
    """Run a frozen Qwen-only evaluation and optionally cache concise results.

    A real run requires an explicit runtime API key. Tests may inject a model
    client. This prevents an unavailable model from being mislabeled as Qwen.
    """
    if model_client is None and not api_key:
        raise ValueError("A runtime Groq API key or injected model client is required")
    if scenario_count < 1:
        raise ValueError("scenario_count must be positive")

    scenarios = ScenarioGenerator(seed=seed).generate(scenario_count)
    summary = QwenEvaluationSummary(seed=seed, scenario_count=len(scenarios), model=model)

    with tempfile.TemporaryDirectory(prefix="waggle-qwen-eval-") as temp_dir:
        root = Path(temp_dir)
        settings = Settings(
            app_db_path=str(root / "app.db"),
            waggle_db_path=str(root / "waggle.db"),
            waggle_embedding_model="fake",
            decision_provider="agent",
            groq_model=model,
            groq_api_key=api_key,
        )
        from waggle.embeddings import EmbeddingModel
        from waggle.graph import MemoryGraph

        graph = MemoryGraph(
            db_path=str(root / "waggle.db"),
            embedding_model=EmbeddingModel("fake"),
            enable_dedup=False,
        )
        adapter = WaggleRecoveryMemoryAdapter(graph.for_tenant("qwen-evaluation"))
        db = Database(str(root / "app.db"))
        provider = AgentDecisionProvider(
            api_key=api_key,
            model=model,
            temperature=0.0,
            model_client=model_client,
        )
        orchestrator = RecoveryOrchestrator(
            adapter=adapter,
            db=db,
            settings=settings,
            decision_provider=provider,
        )

        for index, scenario in enumerate(scenarios):
            if index:
                db.clear_recovery_data()
                adapter.graph.clear_all()
            _populate_memory(adapter, db, orchestrator, scenario)
            event = NormalizedPaymentEvent(
                event_type="payment.failed",
                payment_id=scenario.current_payment_id or f"qwen_{scenario.id}_current",
                customer_id=scenario.customer_id,
                merchant_id=scenario.merchant_id,
                amount=scenario.amount,
                method=scenario.method,
                instrument_id=scenario.instrument_id,
                error_code=scenario.failure_code,
                error_description=scenario.failure_reason,
                created_at=datetime.now(UTC),
                source="qwen_evaluation",
                test_mode=True,
            )
            started = time.perf_counter()
            result = orchestrator.process_event(
                event=event,
                simulation_outcomes=scenario.action_outcomes,
                simulate=True,
                decision_provider=provider,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            trace = result.get("agent_trace") or {}
            fallback = bool(trace.get("agent_fallback", True))
            validation_errors = [str(item) for item in trace.get("validation_errors", [])]
            candidate = {
                "action": trace.get("candidate_action", ""),
                "retry_after_seconds": trace.get("candidate_retry_after_seconds"),
                "recommended_method": trace.get("candidate_recommended_method"),
            }
            candidate_outcome = _outcome_for_decision(scenario, candidate["action"], candidate)
            final = result.get("decision", {})
            final_outcome = str(result.get("outcome", {}).get("outcome", "FAILURE"))
            cited = set(trace.get("cited_evidence_ids", []))
            rejected = set(trace.get("rejected_evidence_ids", []))
            accepted = set(trace.get("accepted_evidence_ids", []))
            forbidden = cited & rejected
            unknown = cited - accepted - rejected
            forbidden_rejected_usage = bool(forbidden) or any(
                "rejected evidence" in item.lower() or "stale or superseded" in item.lower()
                for item in validation_errors
            )
            policy_result = str(final.get("policy_result", "ALLOW"))

            if not fallback:
                summary.structured_output_count += 1
            else:
                summary.fallback_count += 1
            if not fallback and _decision_is_correct(
                scenario, candidate["action"], candidate, candidate_outcome
            ):
                summary.candidate_correct_count += 1
            if _decision_is_correct(scenario, str(final.get("action", "")), final, final_outcome):
                summary.final_correct_count += 1
            if scenario.has_stale_memory:
                summary.stale_scenario_count += 1
                if forbidden_rejected_usage:
                    summary.stale_evidence_citation_count += 1
            if forbidden_rejected_usage:
                summary.forbidden_rejected_evidence_usage_count += 1
            if unknown or any("unknown evidence" in item.lower() for item in validation_errors):
                summary.hallucinated_evidence_count += 1
            if not fallback and policy_result == "MODIFY":
                summary.policy_modification_count += 1
            elif not fallback and policy_result == "BLOCK":
                summary.policy_block_count += 1
            if (
                str(final.get("action")) == "ESCALATE"
                and result.get("outcome", {}).get("recovered_amount", 0) == 0
                and result.get("escalation", {}).get("human_review_required") is True
            ):
                summary.safe_escalation_count += 1
            model_latency = float(trace.get("model_latency_ms", 0.0))
            summary.model_latencies_ms.append(model_latency)
            summary.rows.append({
                "scenario_id": scenario.id,
                "category": scenario.category,
                "candidate": candidate,
                "candidate_correct": _decision_is_correct(
                    scenario, candidate["action"], candidate, candidate_outcome
                ),
                "final_action": final.get("action"),
                "final_correct": _decision_is_correct(
                    scenario, str(final.get("action", "")), final, final_outcome
                ),
                "policy_result": policy_result,
                "fallback": fallback,
                "validation_errors": validation_errors,
                "cited_evidence_ids": sorted(cited),
                "forbidden_rejected_evidence_ids": sorted(forbidden),
                "model_latency_ms": model_latency,
                "total_latency_ms": elapsed_ms,
            })

    if cache_path is not None:
        destination = Path(cache_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(summary.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return summary
