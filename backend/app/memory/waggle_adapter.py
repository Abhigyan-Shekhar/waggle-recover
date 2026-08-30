"""Waggle memory adapter for Waggle Recover.

Stores and retrieves payment domain data using existing Waggle NodeType/RelationType.
Never adds new global enums to Waggle Core.
"""
from __future__ import annotations

import logging
from typing import Any

from waggle.graph import MemoryGraph
from waggle.models import RelationType

from app.domain.models import (
    EscalationRecord,
    MerchantPolicy,
    PaymentFailure,
    PaymentInstrument,
    RecoveryAttempt,
    RecoveryDecision,
    RecoveryExecution,
)
from app.memory import mapper

LOGGER = logging.getLogger(__name__)


class WaggleRecoveryMemoryAdapter:
    """
    Stores and retrieves payment recovery data using Waggle's graph memory.

    Mapping:
    - PaymentFailure → NodeType.FACT + tags["payment_failure", ...]
    - PaymentInstrument → NodeType.ENTITY + tags["payment_instrument", ...]
    - RecoveryDecision → NodeType.DECISION + tags["recovery_decision", ...]
    - RecoveryAttempt/Outcome → NodeType.FACT + tags["recovery_outcome", ...]
    - MerchantPolicy → NodeType.PREFERENCE + tags["merchant_policy", ...]
    """

    def __init__(self, graph: MemoryGraph) -> None:
        self.graph = graph

    # ── Store operations ────────────────────────────────────────────────────

    def store_payment_failure(self, failure: PaymentFailure) -> str:
        """Store a payment failure in Waggle. Returns Waggle node ID."""
        failure_dict = failure.model_dump(mode="json")

        result = self.graph.add_node(
            label=f"Payment failure {failure.external_payment_id}",
            content=mapper.failure_content(failure_dict),
            node_type=mapper.failure_node_type(),
            tags=mapper.failure_tags(failure_dict),
            metadata=mapper.failure_metadata(failure_dict),
            valid_from=failure.occurred_at,
        )
        node_id = result.node.id
        LOGGER.debug("Stored payment failure %s as Waggle node %s", failure.id, node_id)
        return node_id

    def store_payment_instrument(
        self,
        instrument: PaymentInstrument,
        old_instrument_node_id: str | None = None,
    ) -> str:
        """
        Store a payment instrument in Waggle.
        If this instrument supersedes an old one, creates an `updates` edge
        which automatically sets valid_to on the old node.
        Returns Waggle node ID.
        """
        instr_dict = instrument.model_dump(mode="json")

        result = self.graph.add_node(
            label=f"Instrument {instrument.fingerprint_or_safe_alias}",
            content=mapper.instrument_content(instr_dict),
            node_type=mapper.instrument_node_type(),
            tags=mapper.instrument_tags(instr_dict),
            metadata=mapper.instrument_metadata(instr_dict),
            valid_from=instrument.created_at,
        )
        node_id = result.node.id

        # Create supersession edge if this instrument replaces an old one
        if old_instrument_node_id:
            try:
                self.graph.add_edge(
                    source_id=node_id,
                    target_id=old_instrument_node_id,
                    relationship=RelationType.UPDATES,
                    metadata={
                        "supersession": True,
                        "new_instrument": instrument.fingerprint_or_safe_alias,
                        "occurred_at": instrument.created_at.isoformat(),
                    },
                )
                LOGGER.info(
                    "Created supersession edge: %s --updates--> %s",
                    instrument.fingerprint_or_safe_alias,
                    old_instrument_node_id,
                )
            except Exception as e:
                LOGGER.warning("Failed to create supersession edge: %s", e)

        LOGGER.debug("Stored instrument %s as Waggle node %s", instrument.fingerprint_or_safe_alias, node_id)
        return node_id

    def store_recovery_decision(
        self,
        decision: RecoveryDecision,
        failure_node_id: str | None = None,
        customer_id: str = "",
        merchant_id: str = "",
    ) -> str:
        """Store a recovery decision in Waggle. Returns Waggle node ID."""
        dec_dict = decision.model_dump(mode="json")
        dec_dict["customer_id"] = customer_id
        dec_dict["merchant_id"] = merchant_id

        result = self.graph.add_node(
            label=f"Recovery decision {decision.action} for {decision.failure_id[:8]}",
            content=mapper.decision_content(dec_dict),
            node_type=mapper.decision_node_type(),
            tags=mapper.decision_tags(dec_dict),
            metadata=mapper.decision_metadata(dec_dict),
            valid_from=decision.created_at,
        )
        node_id = result.node.id

        # Link decision to failure
        if failure_node_id:
            try:
                self.graph.add_edge(
                    source_id=node_id,
                    target_id=failure_node_id,
                    relationship=RelationType.DEPENDS_ON,
                    metadata={"relation": "decision_for_failure"},
                )
            except Exception as e:
                LOGGER.debug("Could not link decision to failure: %s", e)

        # Persist the evidence provenance that produced this decision. Accepted
        # memories are dependencies; rejected memories are contradictions for
        # this decision because their historical result is no longer valid in
        # the current temporal context. Keeping these as graph edges makes the
        # retrieval and supersession decision auditable after the request ends.
        for reference in decision.evidence_references:
            self._link_decision_evidence(node_id, reference, accepted=True)
        for reference in decision.discarded_evidence:
            self._link_decision_evidence(node_id, reference, accepted=False)
        prior_evidence_ids = {
            evidence_id
            for prior in decision.strategy_priors
            for evidence_id in prior.authoritative_evidence_ids
        }
        for evidence_id in prior_evidence_ids:
            try:
                self.graph.add_edge(
                    source_id=node_id,
                    target_id=evidence_id,
                    relationship=RelationType.DEPENDS_ON,
                    metadata={
                        "relation": "strategy_prior_evidence",
                        "validation_status": "accepted",
                        "authoritative": True,
                    },
                )
            except Exception as e:
                LOGGER.debug("Could not link strategy-prior evidence %s: %s", evidence_id, e)

        LOGGER.debug("Stored decision %s as Waggle node %s", decision.id, node_id)
        return node_id

    def store_recovery_outcome(
        self,
        attempt: RecoveryAttempt,
        decision_node_id: str | None = None,
        failure_node_id: str | None = None,
    ) -> str:
        """Store a recovery outcome in Waggle. Returns Waggle node ID."""
        attempt_dict = attempt.model_dump(mode="json")

        result = self.graph.add_node(
            label=f"Recovery outcome {attempt.outcome} for {attempt.failure_id[:8]}",
            content=mapper.outcome_content(attempt_dict),
            node_type=mapper.outcome_node_type(),
            tags=mapper.outcome_tags(attempt_dict),
            metadata=mapper.outcome_metadata(attempt_dict),
            valid_from=attempt.executed_at,
        )
        node_id = result.node.id

        # Link outcome → decision
        if decision_node_id:
            try:
                self.graph.add_edge(
                    source_id=node_id,
                    target_id=decision_node_id,
                    relationship=RelationType.DERIVED_FROM,
                    metadata={"relation": "outcome_of_decision"},
                )
            except Exception as e:
                LOGGER.debug("Could not link outcome to decision: %s", e)
        if failure_node_id:
            try:
                self.graph.add_edge(
                    source_id=node_id,
                    target_id=failure_node_id,
                    relationship=RelationType.DERIVED_FROM,
                    metadata={"relation": "outcome_of_failure"},
                )
            except Exception as e:
                LOGGER.debug("Could not link outcome to failure: %s", e)

        LOGGER.debug("Stored outcome %s as Waggle node %s", attempt.id, node_id)
        return node_id

    def store_escalation_record(
        self,
        escalation: EscalationRecord,
        decision_node_id: str,
        failure_node_id: str,
    ) -> str:
        """Persist a human-review handoff as a first-class audit node."""
        data = escalation.model_dump(mode="json")
        result = self.graph.add_node(
            label=f"Human review {escalation.id[:8]}",
            content=(
                f"Automated recovery stopped for episode {escalation.recovery_episode_id}. "
                f"Reason: {escalation.escalation_reason}. Money movement: NONE."
            ),
            node_type=mapper.decision_node_type(),
            tags=[
                "escalation_record",
                "human_review",
                f"merchant:{escalation.merchant_id}",
                f"customer:{escalation.customer_id}",
                f"episode:{escalation.recovery_episode_id}",
                f"state:{escalation.state.lower()}",
            ],
            metadata={**data, "money_movement": "NONE"},
            valid_from=escalation.created_at,
        )
        for target_id, relation in (
            (decision_node_id, "escalation_of_decision"),
            (failure_node_id, "escalation_of_failure"),
        ):
            self.graph.add_edge(
                source_id=result.node.id,
                target_id=target_id,
                relationship=RelationType.DERIVED_FROM,
                metadata={"relation": relation, "human_review_required": True},
            )
        return result.node.id

    def store_recovery_execution(
        self,
        execution: RecoveryExecution,
        *,
        decision_node_id: str,
        failure_node_id: str,
    ) -> str:
        """Store a pending provider surface without representing it as recovery."""
        data = execution.model_dump(mode="json")
        result = self.graph.add_node(
            label=f"{execution.provider} {execution.execution_type.lower()} {execution.id[:8]}",
            content=(
                f"Bounded {execution.execution_type} for episode {execution.recovery_episode_id}; "
                f"status {execution.status}. Creation is not recovered revenue."
            ),
            node_type=mapper.outcome_node_type(),
            tags=[
                "recovery_execution", f"provider:{execution.provider}",
                f"episode:{execution.recovery_episode_id}", f"status:{execution.status.lower()}",
            ],
            metadata={**data, "test_mode": execution.provider == "razorpay_test", "money_movement_confirmed": False},
            valid_from=execution.created_at,
        )
        for target_id, relation in (
            (decision_node_id, "execution_of_decision"),
            (failure_node_id, "execution_of_failure"),
        ):
            self.graph.add_edge(
                source_id=result.node.id,
                target_id=target_id,
                relationship=RelationType.DERIVED_FROM,
                metadata={"relation": relation, "status": execution.status},
            )
        return result.node.id

    def store_merchant_policy(self, policy: MerchantPolicy) -> str:
        """Store merchant policy in Waggle. Returns Waggle node ID."""
        policy_dict = policy.model_dump(mode="json")
        old_policy = self.get_merchant_policy_node(policy.merchant_id)

        result = self.graph.add_node(
            label=f"Merchant policy {policy.merchant_id}",
            content=mapper.policy_content(policy_dict),
            node_type=mapper.policy_node_type(),
            tags=mapper.policy_tags(policy_dict),
            metadata=policy_dict,
            valid_from=policy.effective_from,
        )
        if old_policy and old_policy["id"] != result.node.id:
            supersession_edge = self.graph.add_edge(
                source_id=result.node.id,
                target_id=old_policy["id"],
                relationship=RelationType.UPDATES,
                metadata={
                    "relation": "policy_supersession",
                    "new_policy_id": policy.policy_id,
                    "old_policy_id": old_policy.get("metadata", {}).get("policy_id", ""),
                    "authoritative": True,
                },
            )
            # An UPDATES edge is the audit relationship; resolving it is the
            # explicit operation that closes the superseded node's validity
            # window in Waggle.  Retrieval must never depend on rank ordering
            # between two apparently-current policies.
            self.graph.resolve_conflict(
                edge_id=supersession_edge.id,
                winner=result.node.id,
                resolution_note=f"Merchant policy v{policy.version} became authoritative",
            )
        LOGGER.debug("Stored merchant policy for %s as Waggle node %s", policy.merchant_id, result.node.id)
        return result.node.id

    # ── Retrieve operations ────────────────────────────────────────────────

    def get_customer_history(
        self,
        customer_id: str,
        merchant_id: str = "",
        instrument_alias: str = "",
        failure_code: str = "",
        max_nodes: int = 20,
    ) -> list[dict[str, Any]]:
        """Retrieve all relevant history for a customer from Waggle."""
        try:
            # This is an authoritative scope lookup, not semantic search. Scan
            # exact tags first so a large tenant cannot crowd the right
            # customer/merchant history out of a semantic top-N result.
            snapshot = self.graph.get_graph_snapshot()
            nodes: list[dict[str, Any]] = []
            for node in snapshot.get("nodes", []):
                tags = node.get("tags") or []
                # Filter to payment domain nodes
                if any(
                    t in tags
                    for t in ["payment_failure", "recovery_decision", "recovery_outcome", "payment_instrument"]
                ):
                    # Customer history is deliberately merchant-scoped. A
                    # separate merchant-pattern query handles cross-customer learning.
                    if (any(t == f"customer:{customer_id}" for t in tags)
                            and (not merchant_id or f"merchant:{merchant_id}" in tags)):
                        nodes.append(dict(node))
            nodes.sort(key=self._snapshot_event_time, reverse=True)
            return nodes[:max_nodes]
        except Exception as e:
            LOGGER.warning("Waggle exact history lookup failed for customer %s: %s", customer_id, e)
            return []

    def get_instrument_node(self, instrument_alias: str, customer_id: str = "") -> dict[str, Any] | None:
        """Find a payment instrument node in Waggle."""
        query = f"payment instrument {instrument_alias}"
        if customer_id:
            query += f" customer {customer_id}"

        try:
            result = self.graph.query(query=query, max_nodes=5, max_depth=0)
            for node in result.nodes:
                tags = node.tags or []
                if (
                    "payment_instrument" in tags
                    and f"instrument:{instrument_alias}" in tags
                    and (not customer_id or f"customer:{customer_id}" in tags)
                ):
                    return self._node_to_dict(node)
        except Exception as e:
            LOGGER.debug("Could not find instrument node for %s: %s", instrument_alias, e)
        return None

    def get_merchant_policy_node(self, merchant_id: str) -> dict[str, Any] | None:
        """Find merchant policy node in Waggle."""
        query = f"merchant policy {merchant_id}"
        try:
            result = self.graph.query(query=query, max_nodes=10, max_depth=0)
            candidates: list[dict[str, Any]] = []
            for node in result.nodes:
                if "merchant_policy" in (node.tags or []):
                    if f"merchant:{merchant_id}" in (node.tags or []):
                        candidate = self._node_to_dict(node)
                        if not candidate.get("valid_to"):
                            candidates.append(candidate)
            if candidates:
                # Semantic result order is relevance-based, not authority-
                # based. Pick the newest explicit policy version so retrieval
                # remains deterministic even while an invalidation has not yet
                # propagated through a query cache.
                return max(
                    candidates,
                    key=lambda item: (
                        int(item.get("metadata", {}).get("version", 1)),
                        str(item.get("metadata", {}).get("effective_from", "")),
                    ),
                )
        except Exception as e:
            LOGGER.debug("Could not find merchant policy for %s: %s", merchant_id, e)
        return None

    def get_merchant_policy_history(self, merchant_id: str) -> list[dict[str, Any]]:
        """Return every policy version, including invalidated audit history."""
        snapshot = self.graph.get_graph_snapshot()
        policies = [
            dict(node) for node in snapshot.get("nodes", [])
            if "merchant_policy" in (node.get("tags") or [])
            and f"merchant:{merchant_id}" in (node.get("tags") or [])
        ]
        return sorted(
            policies,
            key=lambda item: (
                int(item.get("metadata", {}).get("version", 1)),
                str(item.get("metadata", {}).get("effective_from", "")),
            ),
            reverse=True,
        )

    def get_recovery_outcomes_for_pattern(
        self,
        customer_id: str,
        failure_code: str,
        instrument_alias: str = "",
        max_nodes: int = 10,
    ) -> list[dict[str, Any]]:
        """Get historical recovery outcomes for a similar pattern."""
        query = f"recovery outcome customer {customer_id} {failure_code}"
        if instrument_alias:
            query += f" {instrument_alias}"

        try:
            result = self.graph.query(query=query, max_nodes=max_nodes, max_depth=1)
            outcomes = []
            for node in result.nodes:
                tags = node.tags or []
                if "recovery_outcome" in tags and f"customer:{customer_id}" in tags:
                    outcomes.append(self._node_to_dict(node))
            return outcomes
        except Exception as e:
            LOGGER.debug("Could not get recovery outcomes: %s", e)
            return []

    def get_merchant_pattern_history(self, merchant_id: str, max_nodes: int = 10) -> list[dict[str, Any]]:
        """Retrieve merchant-wide outcome evidence, intentionally across customers."""
        try:
            result = self.graph.query(query=f"recovery outcome merchant {merchant_id}", max_nodes=max_nodes, max_depth=1)
            return [self._node_to_dict(node) for node in result.nodes if "recovery_outcome" in (node.tags or [])
                    and f"merchant:{merchant_id}" in (node.tags or [])]
        except Exception as e:
            LOGGER.debug("Could not get merchant history for %s: %s", merchant_id, e)
            return []

    def get_related_nodes(self, node_id: str, max_depth: int = 2) -> list[dict[str, Any]]:
        """Get all nodes related to a given node."""
        try:
            result = self.graph.get_related(node_id=node_id, max_depth=max_depth)
            return [self._node_to_dict(n) for n in result.nodes]
        except Exception as e:
            LOGGER.debug("Could not get related nodes for %s: %s", node_id, e)
            return []

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Get a single node by ID."""
        try:
            node = self.graph.get_node(node_id)
            return self._node_to_dict(node)
        except Exception as e:
            LOGGER.debug("Could not get node %s: %s", node_id, e)
            return None

    def get_nodes_and_edges_for_decision(self, decision_node_id: str) -> dict[str, Any]:
        """Get the full evidence subgraph for a decision (for the Memory Graph UI)."""
        try:
            # Waggle's public snapshot is used instead of get_related here.
            # The current Waggle release expands directed edges as an undirected
            # neighborhood, then scores them using directed shortest paths. An
            # incoming outcome -> decision edge can therefore raise
            # NetworkXNoPath and previously made this endpoint return an empty
            # graph even though the nodes and edges were stored correctly.
            snapshot = self.graph.get_graph_snapshot()
            all_nodes = {node["id"]: node for node in snapshot.get("nodes", [])}
            if decision_node_id not in all_nodes:
                return {"nodes": [], "edges": [], "message": "Decision node not found in Waggle"}

            all_edges = snapshot.get("edges", [])
            adjacency: dict[str, set[str]] = {node_id: set() for node_id in all_nodes}
            for edge in all_edges:
                source_id = edge.get("source_id")
                target_id = edge.get("target_id")
                if source_id in adjacency and target_id in adjacency:
                    adjacency[source_id].add(target_id)
                    adjacency[target_id].add(source_id)

            included = {decision_node_id}
            frontier = {decision_node_id}
            for _ in range(2):
                frontier = {
                    neighbour
                    for node_id in frontier
                    for neighbour in adjacency.get(node_id, set())
                    if neighbour not in included
                }
                included.update(frontier)

            # Include the instrument nodes named by visible event/evidence nodes
            # so the temporal card_9988 --updates--> card_1234 relationship is
            # present in the decision graph, rather than merely written in text.
            instrument_aliases = {
                str(all_nodes[node_id].get("metadata", {}).get(key))
                for node_id in included
                for key in ("instrument_id", "alias")
                if all_nodes[node_id].get("metadata", {}).get(key)
            }
            for node_id, node in all_nodes.items():
                metadata = node.get("metadata", {})
                if "payment_instrument" in (node.get("tags") or []) and metadata.get("alias") in instrument_aliases:
                    included.add(node_id)

            nodes = [all_nodes[node_id] for node_id in included]
            edges = [
                edge
                for edge in all_edges
                if edge.get("source_id") in included and edge.get("target_id") in included
            ]
            return {"root_id": decision_node_id, "nodes": nodes, "edges": edges}
        except Exception as e:
            LOGGER.exception("Could not get graph for decision %s", decision_node_id)
            return {"nodes": [], "edges": [], "message": str(e)}

    def _link_decision_evidence(self, decision_node_id: str, reference: Any, *, accepted: bool) -> None:
        """Attach one retrieved memory to its decision with validation provenance."""
        if not reference.waggle_node_id:
            return
        try:
            self.graph.add_edge(
                source_id=decision_node_id,
                target_id=reference.waggle_node_id,
                relationship=RelationType.DEPENDS_ON if accepted else RelationType.CONTRADICTS,
                weight=max(0.01, float(reference.relevance_score or 0.0)),
                metadata={
                    "relation": "accepted_evidence" if accepted else "rejected_evidence",
                    "validation_status": "accepted" if accepted else "rejected",
                    "temporal_status": str(reference.temporal_status),
                    "rejection_reason": reference.rejection_reason,
                    "relevance_score": reference.relevance_score,
                },
            )
        except Exception as e:
            LOGGER.warning("Could not link %s evidence %s: %s", "accepted" if accepted else "rejected", reference.waggle_node_id, e)

    def link_nodes(
        self,
        source_id: str,
        target_id: str,
        relationship: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create an edge between two Waggle nodes."""
        try:
            self.graph.add_edge(
                source_id=source_id,
                target_id=target_id,
                relationship=relationship,
                metadata=metadata or {},
            )
        except Exception as e:
            LOGGER.debug("Could not link nodes %s → %s: %s", source_id, target_id, e)

    # ── Lookup-first helpers ────────────────────────────────────────────────

    def lookup_exact_recovery_pattern(
        self,
        customer_id: str,
        merchant_id: str,
        instrument_alias: str,
        failure_code: str,
    ) -> dict[str, Any] | None:
        """
        Lookup-first path: check for a high-confidence direct match.
        Returns the most recent successful outcome node if found.
        """
        try:
            snapshot = self.graph.get_graph_snapshot()
            candidates: list[dict[str, Any]] = []
            for node in snapshot.get("nodes", []):
                tags = node.get("tags") or []
                if (
                    "recovery_outcome" in tags
                    and "outcome:success" in tags
                    and f"customer:{customer_id}" in tags
                    and f"merchant:{merchant_id}" in tags
                    and f"instrument:{instrument_alias}" in tags
                    and f"failure_reason:{failure_code}" in tags
                ):
                    candidates.append(dict(node))
            if candidates:
                node_dict = max(candidates, key=self._snapshot_event_time)
                node_dict["_is_direct_match"] = True
                return node_dict
        except Exception as e:
            LOGGER.debug("Lookup-first failed: %s", e)
        return None

    # ── Helper methods ──────────────────────────────────────────────────────

    def _node_to_dict(self, node: Any) -> dict[str, Any]:
        """Convert a Waggle Node to a plain dict."""
        return {
            "id": node.id,
            "label": node.label,
            "content": node.content,
            "node_type": node.node_type,
            "tags": list(node.tags or []),
            "metadata": dict(node.metadata or {}),
            "valid_from": node.valid_from.isoformat() if node.valid_from else None,
            "valid_to": node.valid_to.isoformat() if node.valid_to else None,
            "created_at": node.created_at.isoformat() if node.created_at else None,
            "updated_at": node.updated_at.isoformat() if node.updated_at else None,
            "similarity_score": node.similarity_score,
            "final_score": node.final_score,
        }

    @staticmethod
    def _snapshot_event_time(node: dict[str, Any]) -> str:
        """Stable chronology for exact snapshot lookups."""
        metadata = node.get("metadata") or {}
        return str(
            metadata.get("executed_at")
            or metadata.get("occurred_at")
            or node.get("valid_from")
            or node.get("created_at")
            or ""
        )
