"""End-to-end orchestrator tests with the full recovery pipeline."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from waggle.embeddings import EmbeddingModel

from app.domain.enums import OutcomeStatus, RecoveryAction, TemporalStatus
from app.domain.models import (
    EvidenceBundle,
    EvidenceReference,
    MerchantPolicy,
    NormalizedPaymentEvent,
    PaymentFailure,
    RecoveryDecision,
)
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
    defaults = {
        "event_type": "payment.failed",
        "payment_id": "pay_test_001",
        "customer_id": "CUST-E2E-001",
        "merchant_id": "MERCH-E2E-001",
        "amount": 100000,
        "method": "card",
        "instrument_id": "card_1234",
        "error_code": "issuer_unavailable",
        "error_description": "Issuer temporarily unavailable",
        "created_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return NormalizedPaymentEvent(**defaults)


class TestOrchestratorBasic:
    @pytest.mark.parametrize("terminal_action", [RecoveryAction.STOP, RecoveryAction.ESCALATE])
    def test_terminal_safety_states_are_irreversible(self, tmp_setup, terminal_action):
        orchestrator, _, db = tmp_setup

        class TerminalProvider:
            mode = "test"

            def decide_with_trace(self, bundle):
                return RecoveryDecision(
                    failure_id=bundle.current_failure.id,
                    action=terminal_action,
                    confidence=1.0,
                    reason="Enter terminal safety state",
                ), {"decision_mode": "test"}

        event = _make_event(payment_id=f"pay_terminal_{terminal_action.value.lower()}")
        first = orchestrator.process_event(event=event, simulate=True, decision_provider=TerminalProvider())
        attempts_before = db.get_attempt_count_for_episode(first["recovery_episode"]["id"])

        second = orchestrator.process_event(event=event, simulate=True)

        assert first["decision"]["action"] == terminal_action.value
        assert second["status"] == "terminal"
        assert second["terminal_state"]["action"] == terminal_action.value
        assert second["terminal_state"]["money_movement"] == "NONE"
        assert db.get_attempt_count_for_episode(first["recovery_episode"]["id"]) == attempts_before

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
    def test_superseded_merchant_policy_remains_audit_only(self, tmp_setup):
        orchestrator, adapter, _ = tmp_setup
        old_policy = MerchantPolicy(
            merchant_id="MERCH-POLICY-CHANGE",
            version=1,
            min_retry_interval_seconds=300,
            allowed_actions=[RecoveryAction.RETRY_AFTER, RecoveryAction.STOP],
        )
        old_node = adapter.store_merchant_policy(old_policy)
        new_policy = MerchantPolicy(
            merchant_id=old_policy.merchant_id,
            version=2,
            supersedes_policy_id=old_policy.policy_id,
            min_retry_interval_seconds=900,
            allowed_actions=[RecoveryAction.SUGGEST_METHOD, RecoveryAction.STOP],
            blocked_methods=["card"],
        )
        new_node = adapter.store_merchant_policy(new_policy)

        loaded = orchestrator._load_merchant_policy(old_policy.merchant_id)
        assert loaded.policy_id == new_policy.policy_id
        assert loaded.version == 2
        assert adapter.get_node(old_node)["valid_to"] is not None

        result = orchestrator.process_event(
            event=_make_event(
                merchant_id=old_policy.merchant_id,
                payment_id="pay_policy_changed",
                instrument_id="card_policy_old",
            ),
            simulate=True,
        )
        assert result["decision"]["action"] == "SUGGEST_METHOD"
        assert result["decision"]["recommended_method"] == "upi"
        graph = adapter.get_nodes_and_edges_for_decision(result["decision_waggle_node"])
        graph_ids = {node["id"] for node in graph["nodes"]}
        assert {old_node, new_node}.issubset(graph_ids)

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
        escalation_nodes = [node for node in graph["nodes"] if "escalation_record" in node.get("tags", [])]
        assert len(escalation_nodes) == 1
        assert escalation_nodes[0]["metadata"]["money_movement"] == "NONE"
        persisted = db.get_escalations("PENDING")
        assert len(persisted) == 1
        assert persisted[0]["recovery_episode_id"] == result["recovery_episode"]["id"]

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

    def test_different_payments_in_same_order_share_episode_budget(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        policy = MerchantPolicy(
            merchant_id="MERCH-ORDER-EPISODE",
            max_recovery_attempts=2,
            allowed_actions=list(RecoveryAction),
        )
        results = []
        for payment_id in ("pay_order_attempt_1", "pay_order_attempt_2", "pay_order_attempt_3"):
            results.append(orchestrator.process_event(
                event=_make_event(
                    payment_id=payment_id,
                    order_id="order_shared_recovery",
                    customer_id="CUST-ORDER-EPISODE",
                    merchant_id=policy.merchant_id,
                ),
                merchant_policy=policy,
                simulation_outcomes={"RETRY_AFTER": "FAILURE", "STOP": "SKIPPED"},
                simulate=True,
            ))

        assert len({item["recovery_episode"]["id"] for item in results}) == 1
        assert [item["metrics"]["attempt_count_for_current_failure"] for item in results] == [0, 1, 2]
        assert results[-1]["decision"]["action"] == "ESCALATE"

    def test_merchant_review_policy_forces_auditable_abstention(self, tmp_setup):
        orchestrator, _, db = tmp_setup
        policy = MerchantPolicy(
            merchant_id="MERCH-REVIEW",
            requires_human_review=True,
            allowed_actions=list(RecoveryAction),
        )
        result = orchestrator.process_event(
            event=_make_event(merchant_id=policy.merchant_id, payment_id="pay_review_required"),
            merchant_policy=policy,
            simulate=True,
        )

        assert result["decision"]["action"] == "ESCALATE"
        assert result["decision"]["abstention_reason"] == "Merchant policy requires human review"
        assert result["outcome"]["recovered_amount"] == 0
        assert db.get_escalations("PENDING")

    def test_low_confidence_policy_forces_review(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        policy = MerchantPolicy(
            merchant_id="MERCH-CONFIDENCE",
            requires_human_review_below_confidence=True,
            min_automatic_confidence=0.95,
            allowed_actions=list(RecoveryAction),
        )
        result = orchestrator.process_event(
            event=_make_event(merchant_id=policy.merchant_id, payment_id="pay_low_confidence"),
            merchant_policy=policy,
            simulate=True,
        )
        assert result["decision"]["action"] == "ESCALATE"
        assert "below merchant threshold" in result["decision"]["abstention_reason"]
        assert result["outcome"]["recovered_amount"] == 0

    def test_no_safe_allowed_action_escalates(self, tmp_setup):
        orchestrator, _, _ = tmp_setup
        policy = MerchantPolicy(merchant_id="MERCH-NO-SAFE", allowed_actions=[])
        result = orchestrator.process_event(
            event=_make_event(merchant_id=policy.merchant_id, payment_id="pay_no_safe"),
            merchant_policy=policy,
            simulate=True,
        )
        assert result["decision"]["action"] == "ESCALATE"
        assert result["decision"]["policy_result"] == "BLOCK"
        assert result["outcome"]["outcome"] == "SKIPPED"

    def test_materially_conflicting_evidence_forces_abstention(self):
        failure = PaymentFailure(
            external_payment_id="pay_conflict",
            customer_id="CUST-CONFLICT",
            merchant_id="MERCH-CONFLICT",
            amount=100000,
            method="card",
            instrument_id="card_conflict",
            failure_code="issuer_unavailable",
        )
        bundle = EvidenceBundle(
            current_failure=failure,
            discarded_evidence=[EvidenceReference(
                waggle_node_id="conflicting-memory",
                label="Contradictory exact outcomes",
                memory_type="recovery_outcome",
                temporal_status=TemporalStatus.CONFLICTING,
                accepted=False,
            )],
        )
        decision = RecoveryDecision(
            failure_id=failure.id,
            action=RecoveryAction.RETRY_AFTER,
            retry_after_seconds=600,
            confidence=0.9,
        )
        RecoveryOrchestrator._annotate_confidence(
            decision,
            bundle,
            MerchantPolicy(merchant_id=failure.merchant_id),
        )
        assert decision.action == RecoveryAction.ESCALATE
        assert decision.abstention_reason == "Authoritative evidence is materially conflicting"

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

    def test_curated_hero_rejects_the_only_old_eight_minute_success(self, tmp_setup):
        """The exact demo fixture must visibly prove rejection, not just validator capability."""
        orchestrator, adapter, db = tmp_setup
        from app.evaluation.generator import ScenarioGenerator, isolate_demo_run
        from app.evaluation.runner import _populate_memory

        fixture = next(
            item for item in ScenarioGenerator(seed=42)._curated_scenarios()
            if item.id == "curated_003"
        )
        scenario = isolate_demo_run(fixture, "hero")
        assert scenario.customer_id.endswith("-Dhero")
        assert scenario.merchant_id.endswith("-Dhero")
        assert all(item.merchant_id == scenario.merchant_id for item in scenario.history)
        _populate_memory(adapter, db, orchestrator, scenario)
        result = orchestrator.process_event(
            event=NormalizedPaymentEvent(
                event_type="payment.failed",
                payment_id="pay_curated_stale_current",
                customer_id=scenario.customer_id,
                merchant_id=scenario.merchant_id,
                amount=scenario.amount,
                method=scenario.method,
                instrument_id=scenario.instrument_id,
                error_code=scenario.failure_code,
                error_description=scenario.failure_reason,
                created_at=datetime.now(UTC),
                source="simulator",
            ),
            simulation_outcomes=scenario.action_outcomes,
            simulate=True,
        )

        assert result["decision"]["action"] == "SUGGEST_METHOD"
        assert result["decision"]["recommended_method"] == "upi"
        assert result["metrics"]["evidence_accepted"] == 0
        rejected = [
            item for item in result["audit"]["discarded_evidence"]
            if item["memory_type"] == "recovery_outcome"
        ]
        assert len(rejected) == 1
        rejected_node = adapter.get_node(rejected[0]["node_id"])
        assert rejected_node["metadata"]["retry_after_seconds"] == 480
        assert "superseded" in rejected[0]["rejection_reason"].lower()

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
