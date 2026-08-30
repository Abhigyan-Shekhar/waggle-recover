"""Supersession validator — traverses Waggle `updates` chains to detect stale evidence.

The `updates` relation in Waggle already sets valid_to on the target node.
This validator traverses the chain and marks evidence as SUPERSEDED when
the underlying instrument/state has been updated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from waggle.graph import MemoryGraph
from waggle.models import Node, RelationType

from app.domain.enums import TemporalStatus
from app.domain.models import EvidenceReference

LOGGER = logging.getLogger(__name__)

MAX_CHAIN_DEPTH = 10  # Prevent infinite traversal


@dataclass
class SupersessionResult:
    """Result of supersession validation for a single evidence item."""
    evidence: EvidenceReference
    temporal_status: TemporalStatus
    update_chain: list[str] = field(default_factory=list)  # node ids in chain
    superseded_by: str | None = None  # alias of superseding instrument
    reason: str = ""


@dataclass
class SupersessionSummary:
    """Summary of supersession validation for an evidence bundle."""
    accepted: list[EvidenceReference] = field(default_factory=list)
    rejected: list[EvidenceReference] = field(default_factory=list)
    supersession_results: list[SupersessionResult] = field(default_factory=list)


class SupersessionValidator:
    """
    Validates temporal relevance of evidence by traversing Waggle `updates` chains.

    The key insight: memory.exists != memory.should_control_decision.
    When a payment instrument has been updated (superseded), historical evidence
    tied to the old instrument must be discarded.
    """

    def __init__(self, graph: MemoryGraph) -> None:
        self.graph = graph

    def validate_evidence_bundle(
        self,
        evidence_refs: list[EvidenceReference],
        current_instrument_alias: str,
        current_instrument_node_id: str | None = None,
        customer_id: str = "",
    ) -> SupersessionSummary:
        """
        Validate all evidence in a bundle against the current instrument state.

        Args:
            evidence_refs: All candidate evidence items
            current_instrument_alias: e.g. "card_9988"
            current_instrument_node_id: Waggle node ID of current instrument

        Returns:
            SupersessionSummary with accepted and rejected lists
        """
        summary = SupersessionSummary()

        for ref in evidence_refs:
            result = self._validate_single(
                ref,
                current_instrument_alias,
                current_instrument_node_id,
                customer_id,
            )
            summary.supersession_results.append(result)

            # Fail closed: only evidence proven CURRENT may enter trusted
            # decision memory. UNKNOWN remains available in the audit trail,
            # but must never control an automated recovery action.
            if result.temporal_status == TemporalStatus.CURRENT:
                ref.temporal_status = result.temporal_status
                ref.accepted = True
                summary.accepted.append(ref)
            else:
                ref.temporal_status = result.temporal_status
                ref.accepted = False
                ref.rejection_reason = result.reason
                summary.rejected.append(ref)

        return summary

    def _validate_single(
        self,
        ref: EvidenceReference,
        current_instrument_alias: str,
        current_instrument_node_id: str | None,
        customer_id: str = "",
    ) -> SupersessionResult:
        """Validate a single evidence reference."""
        try:
            node = self.graph.get_node(ref.waggle_node_id)
        except Exception as e:
            LOGGER.warning("Could not retrieve node %s: %s", ref.waggle_node_id, e)
            return SupersessionResult(
                evidence=ref,
                temporal_status=TemporalStatus.UNKNOWN,
                reason=f"Node retrieval failed: {e}",
            )

        # Check if the node itself is invalidated by valid_to
        if node.valid_to is not None:
            from datetime import UTC, datetime
            now = datetime.now(UTC)
            if node.valid_to < now:
                return SupersessionResult(
                    evidence=ref,
                    temporal_status=TemporalStatus.STALE,
                    reason=f"Node marked invalid at {node.valid_to.isoformat()}",
                )

        # Find what instrument this evidence references
        evidence_instrument = self._extract_instrument_from_node(node)

        if not evidence_instrument:
            # No instrument reference — can't supersede, treat as current
            return SupersessionResult(
                evidence=ref,
                temporal_status=TemporalStatus.CURRENT,
                reason="No instrument reference in evidence",
            )

        # If evidence references the current instrument directly, it's current
        if evidence_instrument == current_instrument_alias:
            return SupersessionResult(
                evidence=ref,
                temporal_status=TemporalStatus.CURRENT,
                reason=f"Evidence references current instrument {current_instrument_alias}",
            )

        # Check if the evidence's instrument has been superseded by traversing updates chain
        # First use the explicit provenance recorded on the current instrument;
        # this remains reliable even when semantic retrieval merges similar nodes.
        if self._instrument_declares_supersedes(current_instrument_alias, evidence_instrument, customer_id):
            return SupersessionResult(
                evidence=ref,
                temporal_status=TemporalStatus.SUPERSEDED,
                update_chain=[evidence_instrument, current_instrument_alias],
                superseded_by=current_instrument_alias,
                reason=f"Evidence tied to {evidence_instrument}, explicitly superseded by {current_instrument_alias}.",
            )
        superseded_by, chain = self._find_superseding_instrument(
            evidence_instrument_alias=evidence_instrument,
            current_instrument_alias=current_instrument_alias,
            customer_id=customer_id,
        )

        if superseded_by:
            return SupersessionResult(
                evidence=ref,
                temporal_status=TemporalStatus.SUPERSEDED,
                update_chain=chain,
                superseded_by=superseded_by,
                reason=(
                    f"Evidence tied to {evidence_instrument} which was superseded by "
                    f"{superseded_by} via updates chain: {' → '.join(chain)}"
                ),
            )

        # A different active instrument is a valid alternative-method signal,
        # not stale evidence. Only an explicit invalidation/update chain rejects.
        return SupersessionResult(
            evidence=ref,
            temporal_status=TemporalStatus.CURRENT,
            reason=(
                f"Evidence tied to active alternative instrument {evidence_instrument}; "
                "no direct supersession chain found."
            ),
        )

    def _instrument_declares_supersedes(self, current_alias: str, old_alias: str, customer_id: str = "") -> bool:
        try:
            result = self.graph.query(query=f"payment instrument {current_alias}", max_nodes=10, max_depth=0)
            for node in result.nodes:
                if f"instrument:{current_alias}" not in (node.tags or []):
                    continue
                if customer_id and f"customer:{customer_id}" not in (node.tags or []):
                    continue
                metadata = node.metadata or {}
                if str(metadata.get("supersedes") or "") == old_alias:
                    return True
                if f"Supersedes instrument {old_alias}" in node.content:
                    return True
        except Exception:
            return False
        return False

    def _extract_instrument_from_node(self, node: Node) -> str | None:
        """Extract instrument alias from node tags or metadata."""
        # Check tags for instrument:<alias>
        for tag in node.tags:
            if tag.startswith("instrument:"):
                return tag.split(":", 1)[1]

        # Check metadata
        metadata = node.metadata or {}
        if "instrument_id" in metadata and metadata["instrument_id"]:
            return str(metadata["instrument_id"])
        if "alias" in metadata and metadata["alias"]:
            return str(metadata["alias"])
        if "recommended_method" in metadata and metadata["recommended_method"]:
            return None  # This is a method name, not an instrument

        # Recovery outcomes may be linked to their originating failure. Follow
        # that provenance edge when older records lack copied instrument tags.
        try:
            related = self.graph.get_related(node_id=node.id, max_depth=1)
            for related_node in related.nodes:
                if "payment_failure" in (related_node.tags or []):
                    for tag in related_node.tags:
                        if tag.startswith("instrument:"):
                            return tag.split(":", 1)[1]
        except Exception:
            pass

        # Check content for common patterns
        content = node.content.lower()
        # Look for "using card_XXXX" pattern
        import re
        match = re.search(r"(?:using|instrument:)\s*(card_\w+|upi_\w+|wallet_\w+|nb_\w+)", content)
        if match:
            return match.group(1)

        return None

    def _find_superseding_instrument(
        self,
        evidence_instrument_alias: str,
        current_instrument_alias: str,
        customer_id: str = "",
    ) -> tuple[str | None, list[str]]:
        """
        Traverse the `updates` chain from the evidence instrument upward.
        Returns (superseding_alias, chain) if supersession is found.

        Protects against cycles and long chains.
        """
        visited: set[str] = set()
        chain = [evidence_instrument_alias]

        # Find the Waggle node for the evidence instrument
        current_alias = evidence_instrument_alias
        depth = 0

        while depth < MAX_CHAIN_DEPTH:
            depth += 1
            instrument_node_id = self._find_instrument_node_id(current_alias, customer_id)

            if not instrument_node_id:
                break

            if instrument_node_id in visited:
                LOGGER.warning("Cycle detected in updates chain at node %s", instrument_node_id)
                break
            visited.add(instrument_node_id)

            # Find nodes that update this one (i.e., have `updates` edge to this node)
            updating_nodes = self._find_updating_nodes(instrument_node_id)

            if not updating_nodes:
                break

            for updating_node in updating_nodes:
                if customer_id and f"customer:{customer_id}" not in (updating_node.tags or []):
                    continue
                updating_alias = self._extract_instrument_from_node(updating_node)
                if updating_alias:
                    chain.append(updating_alias)
                    if updating_alias == current_instrument_alias:
                        return current_instrument_alias, chain
                    current_alias = updating_alias

        return None, chain

    def _find_instrument_node_id(self, instrument_alias: str, customer_id: str = "") -> str | None:
        """Find the Waggle node ID for an instrument alias."""
        try:
            result = self.graph.query(
                query=f"payment instrument {instrument_alias}",
                max_nodes=5,
                max_depth=0,
            )
            for node in result.nodes:
                if "payment_instrument" in node.tags and (
                    not customer_id or f"customer:{customer_id}" in node.tags
                ):
                    for tag in node.tags:
                        if tag == f"instrument:{instrument_alias}":
                            return node.id
        except Exception as e:
            LOGGER.debug("Could not find instrument node for %s: %s", instrument_alias, e)
        return None

    def _find_updating_nodes(self, target_node_id: str) -> list[Node]:
        """Find all nodes that have an `updates` edge pointing to target_node_id."""
        try:
            related = self.graph.get_related(node_id=target_node_id, max_depth=1)
            # Filter edges where relationship=updates and target=target_node_id
            updating_node_ids = {
                edge.source_id
                for edge in related.edges
                if edge.relationship == RelationType.UPDATES.value
                and edge.target_id == target_node_id
            }
            return [n for n in related.nodes if n.id in updating_node_ids]
        except Exception as e:
            LOGGER.debug("Could not find updating nodes for %s: %s", target_node_id, e)
        return []

    def validate_single_node(
        self,
        node_id: str,
        current_instrument_alias: str,
        label: str = "",
        memory_type: str = "unknown",
        customer_id: str = "",
    ) -> SupersessionResult:
        """Convenience method to validate a single node."""
        ref = EvidenceReference(
            waggle_node_id=node_id,
            label=label,
            memory_type=memory_type,
        )
        return self._validate_single(ref, current_instrument_alias, None, customer_id)

    def is_node_superseded(self, node_id: str) -> bool:
        """Quick check if a node has valid_to set (was superseded by Waggle)."""
        try:
            node = self.graph.get_node(node_id)
            if node.valid_to is None:
                return False
            from datetime import UTC, datetime
            return node.valid_to < datetime.now(UTC)
        except Exception:
            return False
