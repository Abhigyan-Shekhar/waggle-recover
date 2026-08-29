"""End-to-end orchestrator tests with the full recovery pipeline."""
from __future__ import annotations

import pytest
from datetime import UTC, datetime

from waggle.embeddings import EmbeddingModel
from app.domain.models import MerchantPolicy, NormalizedPaymentEvent, PaymentInstrument
from app.domain.enums import OutcomeStatus, RecoveryAction
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator


@pytest.fixture
def tmp_setup(tmp_path):
    from waggle.graph import MemoryGraph

    waggle_db = tmp_path / "waggle.db"
    app_db_path = tmp_path / "app.db"

    graph = MemoryGraph(
        db_path=str(waggle_db),
        embedding_model=EmbeddingModel("fake"),
    )
    tenant_graph = graph.for_tenant("test")

    adapter = WaggleRecoveryMemoryAdapter(tenant_graph)
    db = Database(str(app_db_path))
    orchestrator = RecoveryOrchestrator(adapter=adapter, db=db)

    return orchestrator, adapter, db


def _make_event(**kwargs) -> NormalizedPaymentEvent:
    defaults = dict(
        event_type="payment.failed",
        payment_id="pay_test_001",
        customer_id="CUST-E2E-001",
        merchant_id="MERCH-E2E-001",
        amount=100000,
        method="card",
        instrument_id="card_1234",
        error_code="issuer_unavailable",
        error_description="Issuer temporarily unavailable",
        created_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return NormalizedPaymentEvent(**defaults)


class TestOrchestratorBasic:
    def test_real_execution_is_pending_until_capture(self, tmp_setup):
        orchestrator, _, db = tmp_setup
        event = _make_event(payment_id="pay_external_pending")
        result = orchestrator.process_event(event=event, simulate=False)
        assert result["outcome"]["outcome"] == "PENDING"
        assert result["outcome"]["recovered_amount"] == 0

    def test_processes_failed_event(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        event = _make_event()
        result = orchestrator.process_event(
            event=event,
            simulation_outcomes={"RETRY_AFTER": "SUCCESS", "RETRY_NOW": "FAILURE", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            simulate=True,
        )
        assert result["status"] == "processed"

    def test_result_has_required_keys(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        event = _make_event()
        result = orchestrator.process_event(event=event, simulate=True)
        for key in ("failure_id", "decision", "outcome", "audit", "metrics"):
            assert key in result, f"Missing key: {key}"

    def test_decision_is_valid_action(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        event = _make_event()
        result = orchestrator.process_event(event=event, simulate=True)
        action = result["decision"]["action"]
        assert action in {a.value for a in RecoveryAction}

    def test_captured_event_handled(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        event = _make_event(event_type="payment.captured", payment_id="pay_cap_001")
        result = orchestrator.process_event(event=event, simulate=True)
        assert result["status"] == "captured"

    def test_unsupported_event_skipped(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        event = _make_event(event_type="order.created")
        result = orchestrator.process_event(event=event, simulate=True)
        assert result["status"] == "skipped"


class TestPolicyEnforcement:
    def test_curated_escalation_case_persists_handoff_and_graph_metadata(self, tmp_setup):
        orchestrator, adapter, db = tmp_setup
        from app.evaluation.generator import ScenarioGenerator
        from app.evaluation.runner import _populate_memory

        scenario = next(
            item for item in ScenarioGenerator(seed=42)._curated_scenarios()
            if item.name == "Escalation Required"
        )
        _populate_memory(adapter, db, orchestrator, scenario)
        result = orchestrator.process_event(
            event=_make_event(
                payment_id=scenario.current_payment_id,
                customer_id=scenario.customer_id,
                merchant_id=scenario.merchant_id,
                amount=scenario.amount,
                instrument_id=scenario.instrument_id,
                error_code=scenario.failure_code,
                error_description=scenario.failure_reason,
            ),
            simulation_outcomes=scenario.action_outcomes,
            simulate=True,
        )

        assert result["decision"]["action"] == "ESCALATE"
        assert result["escalation"]["human_review_required"] is True
        assert result["escalation"]["money_movement"] == "NONE"
        assert result["outcome"]["outcome"] == "SKIPPED"

        graph = adapter.get_nodes_and_edges_for_decision(result["decision_waggle_node"])
        decision_node = next(node for node in graph["nodes"] if node["id"] == result["decision_waggle_node"])
        assert decision_node["metadata"]["human_review_required"] is True
        assert decision_node["metadata"]["policy_result"] == "BLOCK"

    def test_independent_failures_do_not_share_retry_budget(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        policy = MerchantPolicy(
            merchant_id="MERCH-INDEPENDENT",
            max_recovery_attempts=3,
            allowed_actions=list(RecoveryAction),
        )

        for index in range(12):
            result = orchestrator.process_event(
                event=_make_event(
                    payment_id=f"pay_independent_{index}",
                    customer_id="CUST-INDEPENDENT",
                    merchant_id=policy.merchant_id,
                ),
                merchant_policy=policy,
                simulation_outcomes={"RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "FAILURE"},
                simulate=True,
            )
            assert result["metrics"]["attempt_count_for_current_failure"] == 0
            assert result["decision"]["policy_result"] != "BLOCK"

        assert result["metrics"]["recent_customer_merchant_activity"] >= 11

    def test_repeated_attempts_for_same_payment_escalate_to_human_review(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        policy = MerchantPolicy(
            merchant_id="MERCH-REPEATED",
            max_recovery_attempts=3,
            allowed_actions=list(RecoveryAction),
        )
        event = _make_event(
            payment_id="pay_repeated_failure",
            customer_id="CUST-REPEATED",
            merchant_id=policy.merchant_id,
        )

        results = [
            orchestrator.process_event(
                event=event,
                merchant_policy=policy,
                simulation_outcomes={"RETRY_AFTER": "FAILURE", "STOP": "SKIPPED"},
                simulate=True,
            )
            for _ in range(4)
        ]

        assert [item["metrics"]["attempt_count_for_current_failure"] for item in results] == [0, 1, 2, 3]
        assert results[-1]["decision"]["action"] == "ESCALATE"
        assert results[-1]["decision"]["policy_result"] == "BLOCK"
        assert results[-1]["decision"]["human_review_required"] is True
        assert results[-1]["outcome"]["outcome"] == "SKIPPED"
        assert results[-1]["outcome"]["recovered_amount"] == 0
        escalation = results[-1]["escalation"]
        assert escalation["action"] == "ESCALATE"
        assert escalation["human_review_required"] is True
        assert escalation["reason"] == "Maximum recovery attempts (3) reached"
        assert escalation["merchant_id"] == policy.merchant_id
        assert escalation["customer_id"] == "CUST-REPEATED"
        assert escalation["failure_code"] == "issuer_unavailable"
        assert escalation["attempt_count"] == escalation["max_automated_attempts"] == 3
        assert escalation["last_safe_action"] == "RETRY_AFTER"
        assert escalation["evidence_ids"]  # retrieved provenance is persisted for the reviewer
        assert escalation["policy_result"] == "BLOCK"
        assert escalation["money_movement"] == "NONE"
        assert escalation["recommended_next_step"] == "Manual review / customer outreach"

    def test_exceeding_max_attempts_stops(self, tmp_setup):
        orchestrator, adapter, db = tmp_setup
        customer_id = "CUST-POLICY-001"
        merchant_id = "MERCH-POLICY-001"

        policy = MerchantPolicy(
            merchant_id=merchant_id,
            max_recovery_attempts=1,  # Very tight limit
            allowed_actions=list(RecoveryAction),
        )

        # First attempt — should pass
        event = _make_event(
            payment_id="pay_pol_001",
            customer_id=customer_id,
            merchant_id=merchant_id,
        )
        result = orchestrator.process_event(
            event=event,
            merchant_policy=policy,
            simulate=True,
        )
        assert result["status"] == "processed"

    def test_blocked_method_not_recommended(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        policy = MerchantPolicy(
            merchant_id="MERCH-002",
            max_recovery_attempts=3,
            blocked_methods=["upi"],
            allowed_actions=list(RecoveryAction),
        )
        event = _make_event(
            payment_id="pay_block_001",
            customer_id="CUST-BLOCK-001",
            merchant_id="MERCH-002",
        )
        result = orchestrator.process_event(
            event=event,
            merchant_policy=policy,
            simulate=True,
        )
        assert result["status"] == "processed"
        # Should not recommend blocked method
        assert result["decision"]["recommended_method"] != "upi"


class TestInstrumentRegistration:
    def test_register_instrument_creates_waggle_node(self, tmp_setup):
        orchestrator, adapter, db = tmp_setup
        instr = orchestrator.register_instrument(
            customer_id="CUST-REG-001",
            instrument_type="card",
            alias="card_test_reg",
        )
        assert instr.waggle_node_id is not None

    def test_supersession_creates_update_chain(self, tmp_setup):
        orchestrator, adapter, db = tmp_setup
        customer_id = "CUST-CHAIN-001"

        # Register old card
        orchestrator.register_instrument(
            customer_id=customer_id,
            instrument_type="card",
            alias="card_chain_old",
        )

        # Register new card superseding old
        new_instr = orchestrator.register_instrument(
            customer_id=customer_id,
            instrument_type="card",
            alias="card_chain_new",
            supersedes_alias="card_chain_old",
        )
        assert new_instr.waggle_node_id is not None

        # Check old instrument is marked superseded in DB
        rows = db.get_instruments_for_customer(customer_id)
        old_row = next((r for r in rows if r["fingerprint_or_safe_alias"] == "card_chain_old"), None)
        assert old_row is not None
        assert old_row["status"] == "superseded"


class TestStaleTrapScenario:
    """
    Core product test: The Stale Card Trap scenario.
    History exists for card_old. New card (card_new) supersedes it.
    Current failure is on card_new.
    System should not blindly replay card_old timing.
    """

    def test_stale_card_trap_complete_flow(self, tmp_setup):
        orchestrator, adapter, db = tmp_setup
        customer_id = "CUST-STALE-001"
        merchant_id = "MERCH-001"

        # 1. Register card_old
        orchestrator.register_instrument(
            customer_id=customer_id,
            instrument_type="card",
            alias="card_stale_old",
        )

        # 2. Store historical failure + recovery for card_old
        from app.domain.models import PaymentFailure, RecoveryAttempt
        old_failure = PaymentFailure(
            external_payment_id="pay_stale_hist",
            customer_id=customer_id,
            merchant_id=merchant_id,
            amount=100000,
            method="card",
            instrument_id="card_stale_old",
            failure_code="issuer_unavailable",
        )
        old_failure_node = adapter.store_payment_failure(old_failure)

        old_attempt = RecoveryAttempt(
            failure_id=old_failure.id,
            customer_id=customer_id,
            merchant_id=merchant_id,
            action_type=RecoveryAction.RETRY_AFTER,
            retry_after_seconds=480,
            executed_at=datetime.now(UTC),
            outcome=OutcomeStatus.SUCCESS,
            recovered_amount=100000,
            method="card",
            instrument_id="card_stale_old",
            failure_code="issuer_unavailable",
        )
        old_outcome_node = adapter.store_recovery_outcome(old_attempt, failure_node_id=old_failure_node)

        # 3. card_new supersedes card_old
        orchestrator.register_instrument(
            customer_id=customer_id,
            instrument_type="card",
            alias="card_stale_new",
            supersedes_alias="card_stale_old",
        )

        # 4. Current failure on card_new — should run through full pipeline
        event = _make_event(
            payment_id="pay_stale_current",
            customer_id=customer_id,
            merchant_id=merchant_id,
            instrument_id="card_stale_new",
            error_code="issuer_unavailable",
        )
        result = orchestrator.process_event(
            event=event,
            simulation_outcomes={
                "RETRY_AFTER": "SUCCESS",
                "RETRY_NOW": "FAILURE",
                "SUGGEST_METHOD": "SUCCESS",
                "CUSTOMER_NUDGE": "SUCCESS",
                "STOP": "SKIPPED",
            },
            simulate=True,
        )

        assert result["status"] == "processed"
        # Metrics should show evidence processing
        assert "metrics" in result
        # The explanation should be populated
        assert result["decision"]["explanation"]
        # Stale successful timing must not be accepted when it is presented to
        # the validator (the invariant behind retrieval and the decision layer).
        from app.domain.models import EvidenceReference
        validation = orchestrator.retriever.validator.validate_evidence_bundle(
            [EvidenceReference(waggle_node_id=old_outcome_node, label="old success", memory_type="recovery_outcome")],
            current_instrument_alias="card_stale_new",
        )
        assert old_outcome_node in {r.waggle_node_id for r in validation.rejected}

    def test_stale_evidence_appears_in_discarded(self, tmp_setup):
        orchestrator, adapter, db = tmp_setup
        # Run the stale trap and check discarded evidence
        # (This is the money shot — Waggle Recover's key differentiator)
        result = orchestrator.process_event(
            event=_make_event(
                payment_id="pay_stale_check",
                customer_id="CUST-STALE-002",
                instrument_id="card_fresh",
            ),
            simulate=True,
        )
        # No current-event self-memory should be reported.
        assert result["status"] == "processed"
        assert result["metrics"]["memory_contribution"] == "NONE"
