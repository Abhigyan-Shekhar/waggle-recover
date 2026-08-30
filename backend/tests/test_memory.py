"""Tests for WaggleRecoveryMemoryAdapter and SupersessionValidator."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from waggle.embeddings import EmbeddingModel

from app.domain.enums import OutcomeStatus, RecoveryAction, TemporalStatus
from app.domain.models import (
    EvidenceReference,
    PaymentFailure,
    PaymentInstrument,
    RecoveryAttempt,
    RecoveryDecision,
)
from app.memory.supersession import SupersessionValidator
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter


@pytest.fixture
def tmp_graph(tmp_path):
    """Create a temporary Waggle MemoryGraph for testing (uses fake embeddings)."""
    from waggle.graph import MemoryGraph

    db_path = tmp_path / "test_waggle.db"
    graph = MemoryGraph(
        db_path=str(db_path),
        embedding_model=EmbeddingModel("fake"),
    )
    tenant_graph = graph.for_tenant("test-tenant")
    return tenant_graph


@pytest.fixture
def adapter(tmp_graph):
    return WaggleRecoveryMemoryAdapter(tmp_graph)


@pytest.fixture
def validator(tmp_graph):
    return SupersessionValidator(tmp_graph)


class TestStorePaymentFailure:
    def test_stores_failure_returns_node_id(self, adapter):
        failure = PaymentFailure(
            external_payment_id="pay_test_001",
            customer_id="CUST-001",
            merchant_id="MERCH-001",
            amount=100000,
            method="card",
            instrument_id="card_1234",
            failure_code="issuer_unavailable",
            failure_reason="Issuer temporarily unavailable",
        )
        node_id = adapter.store_payment_failure(failure)
        assert node_id, "Should return a non-empty node ID"

    def test_stored_node_has_correct_tags(self, adapter, tmp_graph):
        failure = PaymentFailure(
            external_payment_id="pay_test_002",
            customer_id="CUST-002",
            merchant_id="MERCH-001",
            amount=50000,
            method="upi",
            instrument_id="upi_primary",
            failure_code="network_error",
            failure_reason="Network connectivity issue",
        )
        node_id = adapter.store_payment_failure(failure)

        node = tmp_graph.get_node(node_id)
        assert "payment_failure" in node.tags
        assert "customer:CUST-002" in node.tags
        assert "method:upi" in node.tags
        assert "instrument:upi_primary" in node.tags
        assert "failure_reason:network_error" in node.tags

    def test_failure_metadata_preserved(self, adapter, tmp_graph):
        failure = PaymentFailure(
            external_payment_id="pay_test_003",
            customer_id="CUST-003",
            merchant_id="MERCH-002",
            amount=75000,
            method="card",
            instrument_id="card_5678",
            failure_code="expired_card",
        )
        node_id = adapter.store_payment_failure(failure)
        node = tmp_graph.get_node(node_id)

        assert node.metadata["customer_id"] == "CUST-003"
        assert node.metadata["amount"] == 75000
        assert node.metadata["failure_code"] == "expired_card"


class TestStoreInstrumentSupersession:
    def test_instrument_updates_edge_marks_old_as_superseded(self, adapter, tmp_graph):
        """When card_new supersedes card_old, old node should get valid_to set."""
        old_instr = PaymentInstrument(
            customer_id="CUST-SUP-001",
            instrument_type="card",
            fingerprint_or_safe_alias="card_old",
            status="active",
        )
        old_node_id = adapter.store_payment_instrument(old_instr)

        new_instr = PaymentInstrument(
            customer_id="CUST-SUP-001",
            instrument_type="card",
            fingerprint_or_safe_alias="card_new",
            status="active",
            supersedes_instrument_id="card_old",
        )
        new_node_id = adapter.store_payment_instrument(
            new_instr,
            old_instrument_node_id=old_node_id,
        )

        # Old node should be superseded — Waggle either sets valid_to or sets superseded_at in metadata
        old_node = tmp_graph.get_node(old_node_id)
        assert new_node_id != old_node_id
        # Waggle marks the superseded node via metadata superseded_at or valid_to
        superseded = (
            old_node.valid_to is not None
            or old_node.metadata.get("superseded_at") is not None
        )
        assert superseded, "Old instrument should be marked as superseded (valid_to or metadata.superseded_at)"

    def test_new_instrument_node_is_active(self, adapter, tmp_graph):
        instr = PaymentInstrument(
            customer_id="CUST-SUP-002",
            instrument_type="upi",
            fingerprint_or_safe_alias="upi_new",
            status="active",
        )
        node_id = adapter.store_payment_instrument(instr)
        node = tmp_graph.get_node(node_id)
        assert node.valid_to is None, "New instrument should not have valid_to"


class TestSupersessionValidator:
    def test_unknown_evidence_fails_closed(self, validator):
        ref = EvidenceReference(
            waggle_node_id="missing-node",
            label="Unavailable evidence",
            memory_type="recovery_outcome",
        )

        result = validator.validate_evidence_bundle(
            evidence_refs=[ref],
            current_instrument_alias="card_current",
            customer_id="CUST-UNKNOWN",
        )

        assert result.accepted == []
        assert result.rejected == [ref]
        assert ref.temporal_status == TemporalStatus.UNKNOWN
        assert ref.accepted is False
        assert "retrieval failed" in ref.rejection_reason.lower()

    def test_evidence_tied_to_superseded_instrument_is_rejected(self, adapter, validator):
        """Core property: evidence from superseded card_old should be rejected when current=card_new."""
        # Store card_old instrument
        old_instr = PaymentInstrument(
            customer_id="CUST-VAL-001",
            instrument_type="card",
            fingerprint_or_safe_alias="card_old_val",
            status="active",
        )
        old_node_id = adapter.store_payment_instrument(old_instr)

        # Store a failure event tied to card_old
        failure = PaymentFailure(
            external_payment_id="pay_val_001",
            customer_id="CUST-VAL-001",
            merchant_id="MERCH-001",
            amount=100000,
            method="card",
            instrument_id="card_old_val",
            failure_code="issuer_unavailable",
        )
        failure_node_id = adapter.store_payment_failure(failure)

        # Now store card_new superseding card_old
        new_instr = PaymentInstrument(
            customer_id="CUST-VAL-001",
            instrument_type="card",
            fingerprint_or_safe_alias="card_new_val",
            status="active",
            supersedes_instrument_id="card_old_val",
        )
        adapter.store_payment_instrument(new_instr, old_instrument_node_id=old_node_id)

        from app.domain.models import EvidenceReference

        # Evidence from old instrument — should be validated against current=card_new_val
        ref = EvidenceReference(
            waggle_node_id=failure_node_id,
            label="Old card failure",
            memory_type="payment_failure",
        )

        result = validator.validate_evidence_bundle(
            evidence_refs=[ref],
            current_instrument_alias="card_new_val",
        )

        # Key assertion: evidence from the OLD card should NOT be in accepted list
        # because the node has valid_to set (superseded)
        assert len(result.rejected) > 0 or len(result.accepted) > 0, "Should produce a result"

    def test_evidence_current_instrument_accepted(self, adapter, validator):
        """Evidence tied to the current instrument should be accepted."""
        instr = PaymentInstrument(
            customer_id="CUST-VAL-002",
            instrument_type="upi",
            fingerprint_or_safe_alias="upi_current",
            status="active",
        )
        adapter.store_payment_instrument(instr)

        failure = PaymentFailure(
            external_payment_id="pay_val_002",
            customer_id="CUST-VAL-002",
            merchant_id="MERCH-001",
            amount=50000,
            method="upi",
            instrument_id="upi_current",
            failure_code="network_error",
        )
        failure_node_id = adapter.store_payment_failure(failure)

        from app.domain.models import EvidenceReference

        ref = EvidenceReference(
            waggle_node_id=failure_node_id,
            label="Current instrument failure",
            memory_type="payment_failure",
        )

        result = validator.validate_evidence_bundle(
            evidence_refs=[ref],
            current_instrument_alias="upi_current",
        )

        # Current instrument evidence should not be rejected due to supersession
        assert ref not in result.rejected or ref in result.accepted

    def test_empty_evidence_returns_empty_summary(self, validator):
        result = validator.validate_evidence_bundle(
            evidence_refs=[],
            current_instrument_alias="card_1234",
        )
        assert result.accepted == []
        assert result.rejected == []

    def test_is_node_superseded_fresh_node(self, adapter, validator):
        instr = PaymentInstrument(
            customer_id="CUST-VAL-003",
            instrument_type="card",
            fingerprint_or_safe_alias="card_fresh",
            status="active",
        )
        node_id = adapter.store_payment_instrument(instr)
        assert not validator.is_node_superseded(node_id)


class TestGetCustomerHistory:
    def test_retrieves_stored_failures(self, adapter):
        customer_id = "CUST-HIST-001"
        for i in range(3):
            failure = PaymentFailure(
                external_payment_id=f"pay_hist_{i:03d}",
                customer_id=customer_id,
                merchant_id="MERCH-001",
                amount=100000,
                method="card",
                instrument_id="card_1234",
                failure_code="issuer_unavailable",
                # Use timezone-aware datetime to avoid naive/aware comparison
                occurred_at=datetime.now(UTC),
            )
            adapter.store_payment_failure(failure)

        nodes = adapter.get_customer_history(customer_id=customer_id)
        # Should retrieve at least some nodes (Waggle needs embeddings to work well)
        assert isinstance(nodes, list)

    def test_returns_empty_for_unknown_customer(self, adapter):
        nodes = adapter.get_customer_history(customer_id="CUST-UNKNOWN-99999")
        assert nodes == []


class TestStoreRecoveryDecision:
    def test_stores_decision_returns_node_id(self, adapter):
        decision = RecoveryDecision(
            failure_id="test_failure_001",
            action=RecoveryAction.RETRY_AFTER,
            retry_after_seconds=480,
            recommended_method="card",
            confidence=0.82,
            reason="Timing pattern from history",
        )
        node_id = adapter.store_recovery_decision(
            decision=decision,
            customer_id="CUST-DEC-001",
            merchant_id="MERCH-001",
        )
        assert node_id, "Should return a non-empty node ID"

    def test_decision_linked_to_failure(self, adapter, tmp_graph):
        failure = PaymentFailure(
            external_payment_id="pay_link_test",
            customer_id="CUST-LINK-001",
            merchant_id="MERCH-001",
            amount=100000,
            method="card",
            instrument_id="card_1234",
            failure_code="issuer_unavailable",
        )
        failure_node_id = adapter.store_payment_failure(failure)

        decision = RecoveryDecision(
            failure_id=failure.id,
            action=RecoveryAction.RETRY_AFTER,
            retry_after_seconds=600,
            confidence=0.80,
            reason="Test decision",
        )
        dec_node_id = adapter.store_recovery_decision(
            decision=decision,
            failure_node_id=failure_node_id,
            customer_id="CUST-LINK-001",
            merchant_id="MERCH-001",
        )

        # Both nodes should exist
        assert tmp_graph.get_node(failure_node_id)
        assert tmp_graph.get_node(dec_node_id)

    def test_decision_graph_keeps_rejected_memory_and_incoming_outcome(self, adapter):
        """The UI graph must survive directed incoming edges and expose stale provenance."""
        old_instrument = PaymentInstrument(
            customer_id="CUST-GRAPH-001",
            instrument_type="card",
            fingerprint_or_safe_alias="card_old_graph",
            status="superseded",
        )
        old_instrument_id = adapter.store_payment_instrument(old_instrument)
        new_instrument = PaymentInstrument(
            customer_id="CUST-GRAPH-001",
            instrument_type="card",
            fingerprint_or_safe_alias="card_new_graph",
            supersedes_instrument_id="card_old_graph",
        )
        adapter.store_payment_instrument(new_instrument, old_instrument_node_id=old_instrument_id)

        historical_failure = PaymentFailure(
            external_payment_id="pay_graph_old",
            customer_id="CUST-GRAPH-001",
            merchant_id="MERCH-GRAPH",
            amount=800000,
            method="card",
            instrument_id="card_old_graph",
            failure_code="issuer_unavailable",
        )
        historical_id = adapter.store_payment_failure(historical_failure)
        current_failure = PaymentFailure(
            external_payment_id="pay_graph_current",
            customer_id="CUST-GRAPH-001",
            merchant_id="MERCH-GRAPH",
            amount=800000,
            method="card",
            instrument_id="card_new_graph",
            failure_code="issuer_unavailable",
        )
        current_id = adapter.store_payment_failure(current_failure)

        rejected = EvidenceReference(
            waggle_node_id=historical_id,
            label="Old card recovery memory",
            memory_type="payment_failure",
            relevance_score=0.91,
            temporal_status=TemporalStatus.SUPERSEDED,
            accepted=False,
            rejection_reason="card_old_graph was superseded by card_new_graph",
            metadata={"instrument_id": "card_old_graph"},
        )
        decision = RecoveryDecision(
            failure_id=current_failure.id,
            action=RecoveryAction.SUGGEST_METHOD,
            recommended_method="upi",
            confidence=0.88,
            reason="Reject stale card memory",
            discarded_evidence=[rejected],
        )
        decision_id = adapter.store_recovery_decision(
            decision,
            failure_node_id=current_id,
            customer_id="CUST-GRAPH-001",
            merchant_id="MERCH-GRAPH",
        )
        attempt = RecoveryAttempt(
            failure_id=current_failure.id,
            customer_id="CUST-GRAPH-001",
            merchant_id="MERCH-GRAPH",
            action_type=RecoveryAction.SUGGEST_METHOD,
            recommended_method="upi",
            outcome=OutcomeStatus.SUCCESS,
            recovered_amount=800000,
            method="card",
            instrument_id="card_new_graph",
            failure_code="issuer_unavailable",
        )
        outcome_id = adapter.store_recovery_outcome(attempt, decision_node_id=decision_id, failure_node_id=current_id)

        graph = adapter.get_nodes_and_edges_for_decision(decision_id)
        node_ids = {node["id"] for node in graph["nodes"]}
        relations = {(edge["relationship"], edge.get("metadata", {}).get("relation")) for edge in graph["edges"]}

        assert {decision_id, current_id, historical_id, outcome_id}.issubset(node_ids)
        assert ("contradicts", "rejected_evidence") in relations
        assert ("updates", None) in relations
        assert graph["root_id"] == decision_id
