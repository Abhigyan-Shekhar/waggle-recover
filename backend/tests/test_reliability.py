"""Adversarial reliability tests for memory identity, provenance, and execution."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph

import app.main  # noqa: F401 - initialize API imports through their production path
from app.api import evaluation as evaluation_api
from app.domain.enums import OutcomeStatus, RecoveryAction, RetrievalMode
from app.domain.models import (
    EvidenceBundle,
    EvidenceReference,
    NormalizedPaymentEvent,
    PaymentFailure,
    PaymentInstrument,
)
from app.evaluation.generator import ScenarioGenerator
from app.evaluation.runner import _populate_memory
from app.memory.retrieval import EvidenceRetriever
from app.memory.supersession import SupersessionValidator
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.decision_engine import DEFAULT_RETRY_SECONDS, DeterministicDecisionProvider
from app.recovery.orchestrator import RecoveryOrchestrator


@pytest.fixture
def tmp_graph(tmp_path):
    graph = MemoryGraph(db_path=str(tmp_path / "waggle.db"), embedding_model=EmbeddingModel("fake"))
    return graph.for_tenant("reliability")


@pytest.fixture
def adapter(tmp_graph):
    return WaggleRecoveryMemoryAdapter(tmp_graph)


@pytest.fixture
def validator(tmp_graph):
    return SupersessionValidator(tmp_graph)


@pytest.fixture
def tmp_setup(tmp_path, adapter):
    db = Database(tmp_path / "app.db")
    return RecoveryOrchestrator(adapter=adapter, db=db), adapter, db


def _event(**updates) -> NormalizedPaymentEvent:
    values = {
        "event_type": "payment.failed",
        "payment_id": "pay_reliability",
        "customer_id": "CUST-REL",
        "merchant_id": "MERCH-REL",
        "amount": 125000,
        "method": "card",
        "instrument_id": "card_1234",
        "error_code": "issuer_unavailable",
        "error_description": "Issuer unavailable",
        "created_at": datetime.now(UTC),
    }
    values.update(updates)
    return NormalizedPaymentEvent(**values)


def test_async_evaluation_yields_event_loop(monkeypatch):
    completed = False

    def blocking(*_args):
        nonlocal completed
        time.sleep(0.08)
        completed = True

    monkeypatch.setattr(evaluation_api, "_run_evaluation_sync_task", blocking)
    async def exercise():
        task = asyncio.create_task(evaluation_api._run_evaluation_task("run", 42, 1, object(), object()))
        await asyncio.sleep(0.01)

        assert not completed
        assert not task.done()
        await task

    asyncio.run(exercise())
    assert completed


def test_capture_writes_confirmed_success_to_waggle(tmp_setup):
    orchestrator, adapter, db = tmp_setup
    initial = orchestrator.process_event(_event(payment_id="pay_capture_truth"), simulate=False)
    pending_node = initial["outcome_waggle_node"]

    captured = orchestrator.process_event(
        _event(event_type="payment.captured", payment_id="pay_capture_truth"),
        simulate=False,
    )

    assert captured["updated_attempts"] == 1
    assert len(captured["outcome_waggle_nodes"]) == 1
    success_node_id = captured["outcome_waggle_nodes"][0]
    assert success_node_id != pending_node
    success_node = adapter.get_node(success_node_id)
    assert "outcome:success" in success_node["tags"]
    assert "instrument:card_1234" in success_node["tags"]
    related = adapter.graph.get_related(node_id=success_node_id, max_depth=1)
    related_ids = {node.id for node in related.nodes}
    assert initial["decision_waggle_node"] in related_ids
    assert initial["failure_waggle_node"] in related_ids
    attempt = db.get_attempts_for_failure(initial["failure_id"])[0]
    assert attempt["outcome"] == "SUCCESS"
    assert attempt["waggle_outcome_node_id"] == success_node_id

    duplicate = orchestrator.process_event(
        _event(event_type="payment.captured", payment_id="pay_capture_truth"),
        simulate=False,
    )
    assert duplicate["updated_attempts"] == 0
    assert duplicate["outcome_waggle_nodes"] == []


def test_same_alias_is_resolved_within_customer(adapter):
    first = PaymentInstrument(customer_id="CUST-A", instrument_type="card", fingerprint_or_safe_alias="card_1234")
    second = PaymentInstrument(customer_id="CUST-B", instrument_type="card", fingerprint_or_safe_alias="card_1234")
    first_node = adapter.store_payment_instrument(first)
    second_node = adapter.store_payment_instrument(second)

    assert adapter.get_instrument_node("card_1234", "CUST-A")["id"] == first_node
    assert adapter.get_instrument_node("card_1234", "CUST-B")["id"] == second_node


def test_supersession_chain_cannot_cross_customer(adapter, validator):
    old_a = adapter.store_payment_instrument(
        PaymentInstrument(customer_id="CUST-A", instrument_type="card", fingerprint_or_safe_alias="card_1234")
    )
    adapter.store_payment_instrument(
        PaymentInstrument(
            customer_id="CUST-A",
            instrument_type="card",
            fingerprint_or_safe_alias="card_9999",
            supersedes_instrument_id="card_1234",
        ),
        old_instrument_node_id=old_a,
    )
    evidence_b = adapter.store_payment_failure(
        PaymentFailure(
            external_payment_id="pay_b",
            customer_id="CUST-B",
            merchant_id="MERCH-1",
            amount=10000,
            method="card",
            instrument_id="card_1234",
            failure_code="issuer_unavailable",
        )
    )

    result = validator.validate_evidence_bundle(
        [EvidenceReference(waggle_node_id=evidence_b, label="B history", memory_type="payment_failure")],
        current_instrument_alias="card_9999",
        customer_id="CUST-B",
    )

    assert [ref.waggle_node_id for ref in result.accepted] == [evidence_b]
    assert result.rejected == []


@pytest.mark.parametrize(
    "tags",
    [
        ["customer:OTHER", "merchant:MERCH-X", "instrument:card_1", "failure_reason:issuer_unavailable"],
        ["customer:CUST-X", "merchant:OTHER", "instrument:card_1", "failure_reason:issuer_unavailable"],
        ["customer:CUST-X", "merchant:MERCH-X", "instrument:card_2", "failure_reason:issuer_unavailable"],
        ["customer:CUST-X", "merchant:MERCH-X", "instrument:card_1", "failure_reason:network_error"],
    ],
)
def test_retry_timing_scope_requires_all_four_dimensions(tmp_graph, tags):
    retriever = EvidenceRetriever(
        adapter=None,
        supersession_validator=SupersessionValidator(tmp_graph),
    )
    failure = PaymentFailure(
        external_payment_id="pay_scope",
        customer_id="CUST-X",
        merchant_id="MERCH-X",
        amount=10000,
        method="card",
        instrument_id="card_1",
        failure_code="issuer_unavailable",
    )
    ref = retriever._node_to_evidence_ref(
        {
            "id": "node",
            "label": "wrong scope",
            "tags": ["recovery_outcome", "outcome:success", *tags],
            "metadata": {"outcome": "SUCCESS", "retry_after_seconds": 300},
        },
        failure,
        "card_1",
    )
    assert ref.metadata["retry_timing_scope_match"] is False


def test_decision_provider_ignores_retry_timing_without_exact_scope():
    failure = PaymentFailure(
        external_payment_id="pay_scope_guard",
        customer_id="CUST-X",
        merchant_id="MERCH-X",
        amount=10000,
        method="card",
        instrument_id="card_1",
        failure_code="issuer_unavailable",
    )
    wrong_scope = EvidenceReference(
        waggle_node_id="wrong-scope-outcome",
        label="Successful retry from another scope",
        memory_type="recovery_outcome",
        metadata={
            "outcome": "SUCCESS",
            "retry_after_seconds": 300,
            "retry_timing_scope_match": False,
        },
    )

    decision = DeterministicDecisionProvider().decide(
        EvidenceBundle(current_failure=failure, accepted_evidence=[wrong_scope])
    )

    assert decision.action == RecoveryAction.RETRY_AFTER
    assert decision.retry_after_seconds == DEFAULT_RETRY_SECONDS
    assert decision.evidence_references == []


def test_evaluation_provenance_exercises_lookup_first(tmp_setup):
    orchestrator, adapter, db = tmp_setup
    scenario = next(item for item in ScenarioGenerator(seed=42)._curated_scenarios() if item.name == "Timing Memory")
    _populate_memory(adapter, db, orchestrator, scenario)

    result = orchestrator.process_event(
        NormalizedPaymentEvent(
            event_type="payment.failed",
            payment_id="pay_lookup_current",
            customer_id=scenario.customer_id,
            merchant_id=scenario.merchant_id,
            amount=scenario.amount,
            method=scenario.method,
            instrument_id=scenario.instrument_id,
            error_code=scenario.failure_code,
            error_description=scenario.failure_reason,
        ),
        simulation_outcomes=scenario.action_outcomes,
        simulate=True,
    )

    assert result["metrics"]["retrieval_mode"] == RetrievalMode.LOOKUP_FIRST
    assert result["decision"]["retry_after_seconds"] in (480, 600)
    assert result["outcome"]["outcome"] == OutcomeStatus.SUCCESS


def test_stale_scenario_rejects_exact_old_instrument(tmp_setup):
    orchestrator, adapter, db = tmp_setup
    scenario = next(item for item in ScenarioGenerator(seed=42)._curated_scenarios() if item.name == "Stale Card Trap")
    _populate_memory(adapter, db, orchestrator, scenario)

    result = orchestrator.process_event(
        NormalizedPaymentEvent(
            event_type="payment.failed",
            payment_id="pay_stale_exact",
            customer_id=scenario.customer_id,
            merchant_id=scenario.merchant_id,
            amount=scenario.amount,
            method=scenario.method,
            instrument_id=scenario.instrument_id,
            error_code=scenario.failure_code,
            error_description=scenario.failure_reason,
        ),
        simulation_outcomes=scenario.action_outcomes,
        simulate=True,
    )

    assert scenario.stale_instrument in result["metrics"]["discarded_instruments"]
    assert scenario.stale_instrument not in result["metrics"]["accepted_instruments"]
    assert result["decision"]["action"] == RecoveryAction.SUGGEST_METHOD
    assert result["outcome"]["outcome"] == OutcomeStatus.SUCCESS
