"""One-case temporal-authority shadow comparison in isolated storage."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph

from app.config import Settings
from app.domain.models import NormalizedPaymentEvent
from app.evaluation.ablations import _baseline_used_stale
from app.evaluation.generator import EvalScenario, ScenarioGenerator
from app.evaluation.runner import _populate_memory
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator


def _scenario(scenario_id: str) -> EvalScenario:
    scenarios = ScenarioGenerator(seed=42)._curated_scenarios()
    by_id = {item.id: item for item in scenarios}
    by_name = {item.name.lower().replace(" ", "_"): item for item in scenarios}
    try:
        return by_id.get(scenario_id) or by_name[scenario_id.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown curated scenario: {scenario_id}") from exc


def _run_once(scenario: EvalScenario, root: Path, enabled: bool) -> dict[str, Any]:
    settings = Settings(
        app_db_path=str(root / "app.db"), waggle_db_path=str(root / "waggle.db"),
        waggle_embedding_model="fake", decision_provider="deterministic",
    )
    graph = MemoryGraph(db_path=str(root / "waggle.db"), embedding_model=EmbeddingModel("fake"))
    adapter = WaggleRecoveryMemoryAdapter(graph.for_tenant("authority-shadow"))
    db = Database(root / "app.db")
    orchestrator = RecoveryOrchestrator(
        adapter=adapter, db=db, settings=settings, temporal_validation_enabled=enabled,
    )
    _populate_memory(adapter, db, orchestrator, scenario)
    event = NormalizedPaymentEvent(
        event_type="payment.failed", payment_id=scenario.current_payment_id or f"shadow_{scenario.id}",
        customer_id=scenario.customer_id, merchant_id=scenario.merchant_id, amount=scenario.amount,
        method=scenario.method, instrument_id=scenario.instrument_id, error_code=scenario.failure_code,
        error_description=scenario.failure_reason, created_at=datetime.now(UTC), source="shadow",
    )
    result = orchestrator.process_event(
        event=event, simulation_outcomes=scenario.action_outcomes, simulate=True,
    )
    row = db.get_decisions_for_failure(result["failure_id"])[0]
    accepted = json.loads(row["evidence_json"])
    rejected = json.loads(row["discarded_json"])
    decision = result["decision"]
    return {
        "retrieved_evidence": [*accepted, *rejected],
        "accepted_evidence": accepted,
        "rejected_evidence": rejected,
        "rejected_evidence_count": len(rejected),
        "final_action": decision["action"],
        "retry_after_seconds": decision.get("retry_after_seconds"),
        "recommended_method": decision.get("recommended_method"),
        "known_stale_evidence_influenced_action": _baseline_used_stale(scenario, decision),
        "simulated_result": result["outcome"],
    }


def run_authority_shadow(scenario_id: str = "curated_003") -> dict[str, Any]:
    scenario = _scenario(scenario_id)
    with tempfile.TemporaryDirectory(prefix="waggle-shadow-") as temp:
        root = Path(temp)
        without = _run_once(scenario, root / "without", False)
        with_validation = _run_once(scenario, root / "with", True)
    def signature(item: dict[str, Any]) -> tuple:
        metadata = item.get("metadata", {})
        return (
            item.get("label"), item.get("memory_type"), metadata.get("instrument_id") or metadata.get("alias"),
            metadata.get("action_type"), metadata.get("retry_after_seconds"), metadata.get("outcome"),
        )
    without_signatures = {signature(item) for item in without["accepted_evidence"]}
    with_signatures = {signature(item) for item in with_validation["accepted_evidence"]}
    removed = [item for item in without["accepted_evidence"] if signature(item) not in with_signatures]
    return {
        "analysis_only": True,
        "persisted_as_recovery_attempt": False,
        "control": "Only temporal validation differs; scenario, retrieval, ranking, provider, policy, retry budget, and outcomes are identical.",
        "scenario": {"id": scenario.id, "name": scenario.name, "amount": scenario.amount},
        "without_authority_validation": without,
        "with_authority_validation": with_validation,
        "diff": {
            "evidence_removed_by_authority_gate": removed,
            "evidence_removed_count": len(without_signatures - with_signatures),
            "action_change": f"{without['final_action']} → {with_validation['final_action']}",
            "safety_impact": (
                "Known superseded evidence was removed from trusted context."
                if removed else "No authority-sensitive evidence changed this case."
            ),
            "simulated_outcome_difference": (
                f"{without['simulated_result']['outcome']} → {with_validation['simulated_result']['outcome']}"
            ),
        },
    }
