"""Two-path evidence retrieval: lookup-first and full contextual."""
from __future__ import annotations

import logging
import time
from typing import Any

from app.domain.enums import MemoryContribution, RetrievalMode, TemporalStatus
from app.domain.models import (
    EvidenceBundle,
    EvidenceReference,
    MerchantPolicy,
    PaymentFailure,
    PaymentInstrument,
)
from app.memory.scoring import score_evidence
from app.memory.supersession import SupersessionValidator
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter

LOGGER = logging.getLogger(__name__)


class EvidenceRetriever:
    """
    Retrieves and validates evidence from Waggle for a payment failure.

    Path A — Lookup-first:
        For cheap high-confidence cases.
        Returns quickly if exact pattern match found.

    Path B — Full contextual:
        Retrieves all relevant history, scores, supersession-validates,
        and returns an EvidenceBundle.
    """

    def __init__(
        self,
        adapter: WaggleRecoveryMemoryAdapter,
        supersession_validator: SupersessionValidator,
        lookup_confidence_threshold: float = 0.75,
        max_evidence_nodes: int = 20,
    ) -> None:
        self.adapter = adapter
        self.validator = supersession_validator
        self.lookup_confidence_threshold = lookup_confidence_threshold
        self.max_evidence_nodes = max_evidence_nodes

    def retrieve(
        self,
        failure: PaymentFailure,
        merchant_policy: MerchantPolicy | None = None,
        current_instruments: list[PaymentInstrument] | None = None,
        retry_count: int = 0,
    ) -> EvidenceBundle:
        """Main retrieval entry point. Tries lookup-first, falls back to full context."""
        start = time.time()

        instruments = current_instruments or []
        current_instrument_alias = failure.instrument_id or ""

        # Path A: Lookup-first
        if current_instrument_alias:
            bundle = self._try_lookup_first(
                failure=failure,
                current_instrument_alias=current_instrument_alias,
                instruments=instruments,
                merchant_policy=merchant_policy,
                retry_count=retry_count,
            )
            if bundle is not None:
                bundle.retrieval_latency_ms = (time.time() - start) * 1000
                return bundle

        # Path B: Full contextual retrieval
        bundle = self._full_contextual_retrieval(
            failure=failure,
            current_instrument_alias=current_instrument_alias,
            instruments=instruments,
            merchant_policy=merchant_policy,
            retry_count=retry_count,
        )
        bundle.retrieval_latency_ms = (time.time() - start) * 1000
        return bundle

    def _try_lookup_first(
        self,
        failure: PaymentFailure,
        current_instrument_alias: str,
        instruments: list[PaymentInstrument],
        merchant_policy: MerchantPolicy | None,
        retry_count: int,
    ) -> EvidenceBundle | None:
        """
        Path A: Check for a high-confidence direct match.
        Returns bundle if found, None to fall through to full retrieval.
        """
        candidate = self.adapter.lookup_exact_recovery_pattern(
            customer_id=failure.customer_id,
            merchant_id=failure.merchant_id,
            instrument_alias=current_instrument_alias,
            failure_code=failure.failure_code,
        )

        if candidate is None:
            return None

        # Validate it's not superseded
        ref = EvidenceReference(
            waggle_node_id=candidate["id"],
            label=candidate["label"],
            memory_type="recovery_outcome",
            relevance_score=0.85,
        )

        validation = self.validator.validate_evidence_bundle(
            evidence_refs=[ref],
            current_instrument_alias=current_instrument_alias,
        )

        if not validation.accepted:
            LOGGER.debug("Lookup-first candidate is superseded — falling through to full retrieval")
            return None

        # Extract confidence from metadata
        confidence = 0.85  # High confidence for direct match

        if confidence < self.lookup_confidence_threshold:
            return None

        LOGGER.debug("Lookup-first HIT for customer %s", failure.customer_id)
        return EvidenceBundle(
            current_failure=failure,
            accepted_evidence=validation.accepted,
            discarded_evidence=validation.rejected,
            merchant_policy=merchant_policy,
            retrieval_mode=RetrievalMode.LOOKUP_FIRST,
            memory_contribution=MemoryContribution.LOOKUP_FIRST,
            current_instruments=instruments,
            retry_count=retry_count,
        )

    def _full_contextual_retrieval(
        self,
        failure: PaymentFailure,
        current_instrument_alias: str,
        instruments: list[PaymentInstrument],
        merchant_policy: MerchantPolicy | None,
        retry_count: int,
    ) -> EvidenceBundle:
        """
        Path B: Full contextual retrieval from Waggle.
        Retrieves all relevant nodes, scores them, applies supersession validation.
        """
        # Gather all relevant nodes
        raw_nodes = self.adapter.get_customer_history(
            customer_id=failure.customer_id,
            merchant_id=failure.merchant_id,
            instrument_alias=current_instrument_alias,
            failure_code=failure.failure_code,
            max_nodes=self.max_evidence_nodes,
        )

        if not raw_nodes:
            LOGGER.debug("No Waggle history for customer %s", failure.customer_id)
            return EvidenceBundle(
                current_failure=failure,
                accepted_evidence=[],
                discarded_evidence=[],
                merchant_policy=merchant_policy,
                retrieval_mode=RetrievalMode.FULL_CONTEXT,
                memory_contribution=MemoryContribution.NONE,
                current_instruments=instruments,
                retry_count=retry_count,
            )

        # Convert to EvidenceReference with scores
        evidence_refs: list[EvidenceReference] = []
        for node in raw_nodes:
            ref = self._node_to_evidence_ref(
                node=node,
                failure=failure,
                current_instrument_alias=current_instrument_alias,
            )
            evidence_refs.append(ref)

        # Apply supersession validation
        validation = self.validator.validate_evidence_bundle(
            evidence_refs=evidence_refs,
            current_instrument_alias=current_instrument_alias,
        )

        memory_contribution = MemoryContribution.NONE
        if validation.accepted:
            memory_contribution = MemoryContribution.FULL_CONTEXT

        LOGGER.debug(
            "Full retrieval for %s: %d accepted, %d discarded",
            failure.customer_id,
            len(validation.accepted),
            len(validation.rejected),
        )

        return EvidenceBundle(
            current_failure=failure,
            accepted_evidence=validation.accepted,
            discarded_evidence=validation.rejected,
            merchant_policy=merchant_policy,
            retrieval_mode=RetrievalMode.FULL_CONTEXT,
            memory_contribution=memory_contribution,
            current_instruments=instruments,
            retry_count=retry_count,
        )

    def _node_to_evidence_ref(
        self,
        node: dict,
        failure: PaymentFailure,
        current_instrument_alias: str,
    ) -> EvidenceReference:
        """Convert a raw Waggle node dict to an EvidenceReference with scores."""
        memory_type = self._detect_memory_type(node)
        score, components = score_evidence(
            node=node,
            failure=failure,
            current_instrument_alias=current_instrument_alias,
        )

        return EvidenceReference(
            waggle_node_id=node["id"],
            label=node.get("label", ""),
            memory_type=memory_type,
            relevance_score=score,
            temporal_status=TemporalStatus.UNKNOWN,  # Will be set by validator
            accepted=True,
            score_components=components,
            metadata=node.get("metadata", {}),
        )

    def _detect_memory_type(self, node: dict) -> str:
        tags = node.get("tags", [])
        if "payment_failure" in tags:
            return "payment_failure"
        if "recovery_outcome" in tags:
            return "recovery_outcome"
        if "recovery_decision" in tags:
            return "recovery_decision"
        if "payment_instrument" in tags:
            return "payment_instrument"
        if "merchant_policy" in tags:
            return "merchant_policy"
        return "unknown"
