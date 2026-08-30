"""Main recovery orchestrator — ties the entire pipeline together.

Flow:
  NormalizedPaymentEvent
    → store failure in Waggle + app DB
    → retrieve evidence (lookup-first or full contextual)
    → supersession validation
    → candidate decision (deterministic or LLM)
    → policy engine validation
    → execute/simulate
    → store outcome back in Waggle
    → return audit trail
"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings
from app.domain.enums import OutcomeStatus, PolicyResult, RecoveryAction
from app.domain.models import (
    EscalationRecord,
    MerchantPolicy,
    NormalizedPaymentEvent,
    PaymentFailure,
    PaymentInstrument,
    RecoveryAttempt,
    RecoveryDecision,
)
from app.memory.retrieval import EvidenceRetriever
from app.memory.supersession import SupersessionValidator
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.decision_engine import DecisionProvider, DeterministicDecisionProvider
from app.recovery.episodes import recovery_episode_for
from app.recovery.executor import RecoveryExecutor
from app.recovery.explanation import build_explanation, build_structured_audit
from app.recovery.policy import PolicyEngine
from app.recovery.risk import assess_revenue_risk
from app.recovery.strategy_priors import get_strategy_priors

LOGGER = logging.getLogger(__name__)

# Default merchant policy applied when no specific policy is found
DEFAULT_MERCHANT_POLICY = MerchantPolicy(
    merchant_id="default",
    max_recovery_attempts=3,
    min_retry_interval_seconds=300,
    max_retry_interval_seconds=3600,
    allowed_actions=[
        RecoveryAction.RETRY_NOW,
        RecoveryAction.RETRY_AFTER,
        RecoveryAction.SUGGEST_METHOD,
        RecoveryAction.CUSTOMER_NUDGE,
        RecoveryAction.STOP,
    ],
)


class RecoveryOrchestrator:
    """
    End-to-end recovery orchestrator.

    Used by both the Razorpay webhook handler and the simulator.
    Both paths produce identical NormalizedPaymentEvent → same pipeline.
    """

    def __init__(
        self,
        adapter: WaggleRecoveryMemoryAdapter,
        db: Database,
        decision_provider: DecisionProvider | None = None,
        settings: Settings | None = None,
        temporal_validation_enabled: bool = True,
    ) -> None:
        self.adapter = adapter
        self.db = db
        self.settings = settings or get_settings()

        validator = SupersessionValidator(adapter.graph)
        self.retriever = EvidenceRetriever(
            adapter=adapter,
            supersession_validator=validator,
            lookup_confidence_threshold=self.settings.lookup_first_confidence_threshold,
            max_evidence_nodes=self.settings.max_evidence_nodes,
            temporal_validation_enabled=temporal_validation_enabled,
        )
        self.policy_engine = PolicyEngine()
        self.executor = RecoveryExecutor()
        self.decision_provider = decision_provider or DeterministicDecisionProvider()

    def process_event(
        self,
        event: NormalizedPaymentEvent,
        merchant_policy: MerchantPolicy | None = None,
        simulation_outcomes: dict[str, Any] | None = None,
        simulate: bool = True,
        decision_provider: DecisionProvider | None = None,
    ) -> dict[str, Any]:
        """
        Main entry point. Processes a normalized payment event through the full pipeline.

        Returns a structured result dict with decision, outcome, and audit trail.
        """
        start_total = time.time()

        if event.event_type == "payment.captured":
            return self._handle_captured(event)

        if event.event_type != "payment.failed":
            return {"status": "skipped", "reason": f"Unsupported event type: {event.event_type}"}

        # 1. Resolve a stable recovery episode before counting attempts.
        episode = recovery_episode_for(event)
        self.db.upsert_recovery_episode(episode.model_dump(mode="json"))
        failure = self._build_failure(event, episode.id)

        # 2. Policy attempts are scoped to this payment failure. Recent
        # customer+merchant activity is separate telemetry and must never make
        # independent payments consume one another's retry budget.
        retry_count = self.db.get_attempt_count_for_episode(episode.id)
        terminal = self.db.get_terminal_state_for_episode(episode.id)
        if terminal is not None:
            action = RecoveryAction(terminal["action_type"])
            escalation_row = self.db.get_escalation_for_episode(episode.id)
            escalation_payload = None
            if action == RecoveryAction.ESCALATE and escalation_row is not None:
                escalation_payload = {
                    "action": action.value,
                    "human_review_required": True,
                    "record_id": escalation_row["id"],
                    "recovery_episode_id": episode.id,
                    "reason": escalation_row["escalation_reason"],
                    "merchant_id": escalation_row["merchant_id"],
                    "customer_id": escalation_row["customer_id"],
                    "failure_code": escalation_row["failure_reason"],
                    "attempt_count": escalation_row["attempts_used"],
                    "max_automated_attempts": escalation_row["max_automated_attempts"],
                    "last_safe_action": escalation_row["candidate_action"],
                    "evidence_ids": json.loads(escalation_row["accepted_evidence_json"]),
                    "rejected_evidence_ids": json.loads(escalation_row["rejected_evidence_json"]),
                    "policy_result": escalation_row["policy_result"],
                    "money_movement": "NONE",
                    "recommended_next_step": escalation_row["recommended_manual_next_step"],
                    "state": escalation_row["state"],
                }
            # STOP and ESCALATE are absorbing states. Replayed webhooks or new
            # failures in the same episode cannot restart automated recovery.
            return {
                "status": "terminal",
                "reason": "Recovery episode is already in an irreversible safety state",
                "recovery_episode": episode.model_dump(mode="json"),
                "decision": {
                    "action": action.value,
                    "reason": terminal.get("reason") or "Terminal safety state retained",
                    "status": terminal.get("status") or "terminal",
                    "human_review_required": bool(terminal.get("human_review_required")),
                    "escalation_reason": terminal.get("escalation_reason") or "",
                    "attempt_count": terminal.get("attempt_count") or retry_count,
                    "max_automated_attempts": terminal.get("max_automated_attempts") or 0,
                    "last_safe_action": terminal.get("last_safe_action"),
                    "policy_result": terminal.get("policy_result") or PolicyResult.ALLOW.value,
                },
                "outcome": {"outcome": OutcomeStatus.SKIPPED.value, "recovered_amount": 0},
                "terminal_state": {
                    "action": action.value,
                    "entered_at": terminal.get("executed_at"),
                    "decision_id": terminal.get("decision_id"),
                    "money_movement": "NONE",
                },
                "escalation": escalation_payload,
                "metrics": {
                    "attempt_count_for_current_failure": retry_count,
                    "evidence_accepted": 0,
                    "evidence_discarded": 0,
                },
            }
        recent_customer_merchant_activity = self.db.get_recent_customer_merchant_activity(
            failure.customer_id,
            failure.merchant_id,
        )

        # 3. Get merchant policy
        policy = merchant_policy or self._load_merchant_policy(failure.merchant_id)

        # 4. Get current instruments for this customer
        instruments = self._load_instruments(failure.customer_id)

        # 5. Retrieve historical evidence before writing this current event;
        # otherwise a brand-new failure can falsely count as its own memory.
        bundle = self.retriever.retrieve(
            failure=failure,
            merchant_policy=policy,
            current_instruments=instruments,
            retry_count=retry_count,
        )
        bundle.strategy_priors = get_strategy_priors(bundle, self.adapter, self.settings)

        # 6. Store current failure after retrieval so it can still anchor the
        # decision/outcome graph without contaminating historical evidence.
        waggle_node_id = self.adapter.store_payment_failure(failure)
        failure_dict = failure.model_dump(mode="json")
        failure_dict["waggle_node_id"] = waggle_node_id
        failure_dict["created_at"] = datetime.now(UTC).isoformat()
        self.db.upsert_failure(failure_dict)

        # 7. Stage 1: Candidate decision
        decision_start = time.time()
        active_provider = decision_provider or self.decision_provider
        candidate, decision_trace = active_provider.decide_with_trace(bundle)
        provider_candidate_action = candidate.action
        candidate.recovery_episode_id = episode.id
        self._annotate_confidence(candidate, bundle, policy)
        risk_assessment = assess_revenue_risk(bundle)
        candidate.risk_score = risk_assessment.score
        candidate.risk_band = risk_assessment.band
        candidate.risk_factors = risk_assessment.factors
        candidate.strategy_priors = bundle.strategy_priors
        decision_latency_ms = (time.time() - decision_start) * 1000

        # A provider's quiet STOP at the final automated attempt is promoted
        # to an explicit human handoff before either terminal state is stored.
        # Once persisted, both STOP and ESCALATE remain irreversible.
        if (
            candidate.action == RecoveryAction.STOP
            and retry_count >= max(0, policy.max_recovery_attempts - 1)
        ):
            candidate.action = RecoveryAction.ESCALATE
            candidate.abstention_reason = f"Maximum recovery attempts ({policy.max_recovery_attempts}) reached"
            candidate.reason = candidate.abstention_reason
            candidate.retry_after_seconds = None
            candidate.recommended_method = None

        # 8. Stage 2: Policy validation
        policy_result = self.policy_engine.validate(
            decision=candidate,
            bundle=bundle,
            retry_count=retry_count,
            merchant_policy=policy,
        )

        # Apply policy modifications
        candidate_action = provider_candidate_action
        final_decision = self._apply_policy(candidate, policy_result)
        final_decision.policy_result = policy_result.result
        final_decision.policy_note = policy_result.formatted()
        if final_decision.action == RecoveryAction.ESCALATE:
            final_decision.human_review_required = True
            final_decision.escalation_reason = policy_result.block_reason or "No safe automated recovery remains"
            final_decision.attempt_count = (
                retry_count + 1
                if candidate.abstention_reason.startswith("Maximum recovery attempts")
                else retry_count
            )
            final_decision.max_automated_attempts = policy.max_recovery_attempts
            last_action = candidate_action
            if last_action in (RecoveryAction.STOP, RecoveryAction.ESCALATE):
                persisted_action = self.db.get_last_attempt_action_for_episode(episode.id)
                if persisted_action:
                    last_action = RecoveryAction(persisted_action)
            final_decision.last_safe_action = last_action
            final_decision.status = "human_review_required"

        decision_trace.update({
            "policy_result": policy_result.result.value,
            "final_action": final_decision.action.value,
            "final_retry_after_seconds": final_decision.retry_after_seconds,
            "final_recommended_method": final_decision.recommended_method,
        })
        if decision_trace.get("decision_mode") == "agent":
            decision_trace["stages"] = [
                *decision_trace.get("stages", []),
                {
                    "key": "policy_guard",
                    "label": "Policy Guard",
                    "status": "complete" if policy_result.result == PolicyResult.ALLOW else "warning",
                    "detail": f"{policy_result.result.value}: deterministic merchant policy remained final authority.",
                },
                {
                    "key": "final_action",
                    "label": "Final Action",
                    "status": "complete",
                    "detail": self._trace_action_summary(final_decision),
                },
            ]

        # 9. Build explanation
        explanation = build_explanation(bundle, final_decision, policy_result)
        final_decision.explanation = explanation

        # 10. Store decision in Waggle + app DB
        dec_node_id = self.adapter.store_recovery_decision(
            decision=final_decision,
            failure_node_id=waggle_node_id,
            customer_id=failure.customer_id,
            merchant_id=failure.merchant_id,
        )
        final_decision.waggle_node_id = dec_node_id
        current_policy_node = self.adapter.get_merchant_policy_node(failure.merchant_id)
        if current_policy_node:
            self.adapter.link_nodes(
                dec_node_id,
                current_policy_node["id"],
                "depends_on",
                {"relation": "current_merchant_policy", "validation_status": "accepted", "authoritative": True},
            )

        escalation_record = None
        if final_decision.human_review_required:
            escalation_record = self._build_escalation_record(bundle, final_decision, candidate_action)
            escalation_record.waggle_node_id = self.adapter.store_escalation_record(
                escalation_record,
                decision_node_id=dec_node_id,
                failure_node_id=waggle_node_id,
            )
            escalation_dict = escalation_record.model_dump(mode="json")
            escalation_dict["accepted_evidence_json"] = json.dumps(escalation_record.accepted_evidence_ids)
            escalation_dict["rejected_evidence_json"] = json.dumps(escalation_record.rejected_evidence_ids)
            self.db.upsert_escalation(escalation_dict)

        dec_dict = final_decision.model_dump(mode="json")
        dec_dict["evidence_json"] = json.dumps([r.model_dump(mode="json") for r in bundle.accepted_evidence])
        dec_dict["discarded_json"] = json.dumps([r.model_dump(mode="json") for r in bundle.discarded_evidence])
        dec_dict["retrieval_mode"] = bundle.retrieval_mode
        dec_dict["waggle_node_id"] = dec_node_id
        dec_dict["created_at"] = datetime.now(UTC).isoformat()
        dec_dict["risk_factors_json"] = json.dumps(final_decision.risk_factors)
        dec_dict["decision_mode"] = decision_trace.get("decision_mode", "deterministic")
        dec_dict["execution_mode"] = "simulation" if simulate else (
            "live_agent" if decision_trace.get("decision_mode") == "agent" else "live_recommendation"
        )
        dec_dict["decision_latency_ms"] = round(decision_latency_ms, 2)
        self.db.upsert_decision(dec_dict)

        # 11. Execute/simulate the action
        attempt = self.executor.execute(
            decision=final_decision,
            customer_id=failure.customer_id,
            merchant_id=failure.merchant_id,
            failure_id=failure.id,
            original_amount=failure.amount,
            method=failure.method,
            instrument_id=failure.instrument_id,
            failure_code=failure.failure_code,
            recovery_episode_id=episode.id,
            simulate=simulate,
            simulation_outcomes=simulation_outcomes,
        )

        # 12. Store outcome in Waggle + app DB
        outcome_node_id = self.adapter.store_recovery_outcome(
            attempt=attempt,
            decision_node_id=dec_node_id,
            failure_node_id=waggle_node_id,
        )
        attempt.waggle_outcome_node_id = outcome_node_id

        attempt_dict = attempt.model_dump(mode="json")
        attempt_dict["waggle_outcome_node_id"] = outcome_node_id
        self.db.upsert_attempt(attempt_dict)

        # 13. Update instrument success timestamp if recovery succeeded
        if attempt.outcome == OutcomeStatus.SUCCESS and failure.instrument_id:
            self._update_instrument_success(failure.customer_id, failure.instrument_id)

        total_latency_ms = (time.time() - start_total) * 1000

        # 14. Build audit record
        audit = build_structured_audit(bundle, final_decision, policy_result)
        audit["risk_assessment"] = risk_assessment.to_dict()
        audit["strategy_adaptation"] = self._strategy_adaptation_audit(bundle)
        if decision_trace.get("decision_mode") == "agent":
            audit["agent_trace"] = decision_trace

        return {
            "status": "processed",
            "failure_id": failure.id,
            "failure_waggle_node": waggle_node_id,
            "decision": final_decision.model_dump(mode="json"),
            "decision_waggle_node": dec_node_id,
            "outcome": attempt.model_dump(mode="json"),
            "outcome_waggle_node": outcome_node_id,
            "audit": audit,
            "decision_mode": decision_trace.get("decision_mode", "deterministic"),
            "agent_trace": decision_trace if decision_trace.get("decision_mode") == "agent" else None,
            "strategy_priors": [item.model_dump(mode="json") for item in bundle.strategy_priors],
            "risk_assessment": risk_assessment.to_dict(),
            "recovery_episode": episode.model_dump(mode="json"),
            "escalation": self._escalation_payload(escalation_record) if escalation_record else None,
            "metrics": {
                "total_latency_ms": round(total_latency_ms, 2),
                "decision_latency_ms": round(decision_latency_ms, 2),
                "retrieval_latency_ms": round(bundle.retrieval_latency_ms, 2),
                "evidence_accepted": len(bundle.accepted_evidence),
                "evidence_discarded": len(bundle.discarded_evidence),
                "accepted_instruments": self._evidence_instruments(bundle.accepted_evidence),
                "discarded_instruments": self._evidence_instruments(bundle.discarded_evidence),
                "retrieval_mode": bundle.retrieval_mode,
                "memory_contribution": bundle.memory_contribution,
                "attempt_count_for_current_failure": retry_count,
                "recent_customer_merchant_activity": recent_customer_merchant_activity,
            },
        }

    @staticmethod
    def _evidence_instruments(references: list) -> list[str]:
        instruments = set()
        for ref in references:
            metadata = ref.metadata or {}
            instrument = (
                metadata.get("alias")
                if ref.memory_type == "payment_instrument"
                else metadata.get("instrument_id") or metadata.get("alias")
            )
            if instrument:
                instruments.add(str(instrument))
        return sorted(instruments)

    @staticmethod
    def _strategy_adaptation_audit(bundle) -> dict[str, Any]:
        priors = bundle.strategy_priors
        preferred = priors[0] if priors else None
        return {
            "label": "Online evidence-weighted strategy adaptation",
            "priors": [item.model_dump(mode="json") for item in priors],
            "preferred_safe_strategy": None if preferred is None else {
                "action": preferred.action.value,
                "recommended_method": preferred.recommended_method,
                "posterior_success_probability": preferred.posterior_success_probability,
                "insufficient_history": preferred.insufficient_history,
            },
            "merchant_history_insufficient": not priors or all(item.insufficient_history for item in priors),
        }

    def _handle_captured(self, event: NormalizedPaymentEvent) -> dict[str, Any]:
        """Handle payment.captured — close any open recovery attempts."""
        LOGGER.info("Payment captured: %s", event.payment_id)
        # A capture is new durable evidence, not merely a SQLite status flip.
        # Store a confirmed SUCCESS outcome with links back to the original
        # decision and failure, then atomically close that attempt in app data.
        candidates = self.db.get_capture_candidates(event.payment_id)
        updated = 0
        outcome_nodes: list[str] = []
        for row in candidates:
            attempt = RecoveryAttempt(
                id=row["id"],
                failure_id=row["failure_id"],
                customer_id=row["customer_id"],
                merchant_id=row["merchant_id"],
                action_type=RecoveryAction(row["action_type"]),
                recommended_method=row.get("recommended_method"),
                recommended_route=row.get("recommended_route"),
                retry_after_seconds=row.get("retry_after_seconds"),
                decision_id=row.get("decision_id", ""),
                executed_at=event.created_at,
                outcome=OutcomeStatus.SUCCESS,
                recovered_amount=event.amount,
                method=row.get("failure_method", ""),
                instrument_id=row.get("failure_instrument_id", ""),
                failure_code=row.get("failure_code", ""),
                recovery_episode_id=row.get("recovery_episode_id", ""),
            )
            outcome_node_id = self.adapter.store_recovery_outcome(
                attempt=attempt,
                decision_node_id=row.get("decision_waggle_node_id"),
                failure_node_id=row.get("failure_waggle_node_id"),
            )
            if self.db.mark_attempt_captured(attempt.id, event.amount, outcome_node_id):
                updated += 1
                outcome_nodes.append(outcome_node_id)
        return {
            "status": "captured",
            "payment_id": event.payment_id,
            "updated_attempts": updated,
            "recovered_amount": event.amount,
            "outcome_waggle_nodes": outcome_nodes,
        }

    def _build_failure(self, event: NormalizedPaymentEvent, recovery_episode_id: str) -> PaymentFailure:
        return PaymentFailure(
            external_payment_id=event.payment_id,
            order_id=event.order_id,
            customer_id=event.customer_id,
            merchant_id=event.merchant_id,
            amount=event.amount,
            currency=event.currency,
            method=event.method,
            instrument_id=event.instrument_id,
            route=event.route,
            failure_code=event.error_code,
            failure_reason=event.error_description,
            failure_source=event.error_source,
            failure_step=event.error_step,
            occurred_at=event.created_at,
            raw_event_id=event.event_id or event.payment_id,
            recovery_episode_id=recovery_episode_id,
        )

    def _load_merchant_policy(self, merchant_id: str) -> MerchantPolicy:
        """Load merchant policy from Waggle or return default."""
        node = self.adapter.get_merchant_policy_node(merchant_id)
        if node:
            meta = node.get("metadata", {})
            try:
                return MerchantPolicy(
                    policy_id=meta.get("policy_id", "") or meta.get("id", "") or MerchantPolicy(merchant_id=merchant_id).policy_id,
                    merchant_id=merchant_id,
                    version=meta.get("version", 1),
                    effective_from=meta.get("effective_from", datetime.now(UTC)),
                    supersedes_policy_id=meta.get("supersedes_policy_id"),
                    max_recovery_attempts=meta.get("max_recovery_attempts", 3),
                    min_retry_interval_seconds=meta.get("min_retry_interval_seconds", 300),
                    max_retry_interval_seconds=meta.get("max_retry_interval_seconds", 3600),
                    allowed_actions=meta.get("allowed_actions", DEFAULT_MERCHANT_POLICY.allowed_actions),
                    blocked_methods=meta.get("blocked_methods", []),
                    blocked_routes=meta.get("blocked_routes", []),
                    cooldown_seconds=meta.get("cooldown_seconds", 600),
                    requires_human_review=meta.get("requires_human_review", False),
                    requires_human_review_below_confidence=meta.get("requires_human_review_below_confidence", False),
                    min_automatic_confidence=meta.get("min_automatic_confidence", 0.60),
                )
            except Exception:
                pass
        return DEFAULT_MERCHANT_POLICY.model_copy(update={"merchant_id": merchant_id})

    def _load_instruments(self, customer_id: str) -> list[PaymentInstrument]:
        """Load current instruments for a customer from app DB."""
        rows = self.db.get_instruments_for_customer(customer_id)
        instruments = []
        for row in rows:
            try:
                instruments.append(PaymentInstrument(
                    id=row["id"],
                    customer_id=row["customer_id"],
                    instrument_type=row["instrument_type"],
                    fingerprint_or_safe_alias=row["fingerprint_or_safe_alias"],
                    status=row["status"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    supersedes_instrument_id=row.get("supersedes_instrument_id"),
                    waggle_node_id=row.get("waggle_node_id"),
                ))
            except Exception as e:
                LOGGER.debug("Could not parse instrument row: %s", e)
        return instruments

    def _apply_policy(
        self, candidate: RecoveryDecision, policy_result
    ) -> RecoveryDecision:
        """Apply policy ALLOW/MODIFY/BLOCK to the candidate decision."""
        if policy_result.result == PolicyResult.ALLOW:
            return candidate

        if policy_result.result == PolicyResult.MODIFY:
            if policy_result.modified_action:
                candidate.action = policy_result.modified_action
            if policy_result.modified_retry_seconds is not None:
                candidate.retry_after_seconds = policy_result.modified_retry_seconds
            if policy_result.modified_recommended_method is not None:
                candidate.recommended_method = policy_result.modified_recommended_method
            candidate.reason += f" [Policy modified: {policy_result.result}]"
            return candidate

        if policy_result.result == PolicyResult.BLOCK:
            # A policy BLOCK means autonomous recovery is no longer permitted.
            # Do not turn it into a quiet STOP: preserve an auditable, explicit
            # handoff for a human operator and ensure the executor moves no money.
            candidate.action = RecoveryAction.ESCALATE
            candidate.retry_after_seconds = None
            candidate.recommended_method = None
            candidate.recommended_route = None
            candidate.reason = f"Human review required: {policy_result.block_reason}"
            candidate.confidence = 1.0
            return candidate

        return candidate

    @staticmethod
    def _escalation_payload(record: EscalationRecord) -> dict[str, Any]:
        """Stable audit-first handoff payload; no external ticket is created."""
        return {
            "action": RecoveryAction.ESCALATE.value,
            "human_review_required": True,
            "record_id": record.id,
            "recovery_episode_id": record.recovery_episode_id,
            "reason": record.escalation_reason,
            "merchant_id": record.merchant_id,
            "customer_id": record.customer_id,
            "failure_code": record.failure_reason,
            "attempt_count": record.attempts_used,
            "max_automated_attempts": record.max_automated_attempts,
            "last_safe_action": record.candidate_action.value,
            "evidence_ids": record.accepted_evidence_ids,
            "rejected_evidence_ids": record.rejected_evidence_ids,
            "policy_result": record.policy_result.value,
            "money_movement": "NONE",
            "recommended_next_step": record.recommended_manual_next_step,
            "state": record.state,
        }

    @staticmethod
    def _build_escalation_record(bundle, decision: RecoveryDecision, candidate_action: RecoveryAction) -> EscalationRecord:
        failure = bundle.current_failure
        return EscalationRecord(
            recovery_episode_id=decision.recovery_episode_id,
            failure_id=failure.id,
            decision_id=decision.id,
            merchant_id=failure.merchant_id,
            customer_id=failure.customer_id,
            amount=failure.amount,
            failure_reason=failure.failure_code or failure.failure_reason,
            attempts_used=decision.attempt_count,
            max_automated_attempts=decision.max_automated_attempts,
            candidate_action=decision.last_safe_action or candidate_action,
            policy_result=decision.policy_result,
            escalation_reason=decision.escalation_reason,
            accepted_evidence_ids=[ref.waggle_node_id for ref in bundle.accepted_evidence],
            rejected_evidence_ids=[ref.waggle_node_id for ref in bundle.discarded_evidence],
        )

    @staticmethod
    def _annotate_confidence(decision: RecoveryDecision, bundle, policy: MerchantPolicy) -> None:
        scores = [float(ref.relevance_score or 0) for ref in bundle.accepted_evidence]
        decision.evidence_confidence = round(max(scores, default=0.0), 3)
        if not scores:
            decision.evidence_quality = "NONE"
            decision.uncertainty_reason = "No authoritative historical evidence was available"
        elif decision.evidence_confidence >= 0.80:
            decision.evidence_quality = "HIGH"
        elif decision.evidence_confidence >= 0.60:
            decision.evidence_quality = "MEDIUM"
        else:
            decision.evidence_quality = "LOW"
            decision.uncertainty_reason = "Authoritative evidence relevance is weak"

        materially_conflicting = any(ref.temporal_status.value == "CONFLICTING" for ref in bundle.discarded_evidence)
        review_reason = ""
        if policy.requires_human_review:
            review_reason = "Merchant policy requires human review"
        elif materially_conflicting:
            review_reason = "Authoritative evidence is materially conflicting"
        elif policy.requires_human_review_below_confidence and decision.confidence < policy.min_automatic_confidence:
            review_reason = (
                f"Decision confidence {decision.confidence:.0%} is below merchant threshold "
                f"{policy.min_automatic_confidence:.0%}"
            )
        if review_reason:
            decision.action = RecoveryAction.ESCALATE
            decision.abstention_reason = review_reason
            decision.reason = review_reason

    @staticmethod
    def _trace_action_summary(decision: RecoveryDecision) -> str:
        if decision.retry_after_seconds is not None:
            return f"{decision.action.value} after {decision.retry_after_seconds}s"
        if decision.recommended_method:
            return f"{decision.action.value} → {decision.recommended_method.upper()}"
        return decision.action.value

    def _update_instrument_success(self, customer_id: str, instrument_id: str) -> None:
        """Update last_success_at for an instrument after successful recovery."""
        now = datetime.now(UTC).isoformat()
        self.db.execute_write(
            """
            UPDATE payment_instruments
            SET last_success_at = ?
            WHERE customer_id = ? AND fingerprint_or_safe_alias = ?
            """,
            (now, customer_id, instrument_id),
        )

    def register_instrument(
        self,
        customer_id: str,
        instrument_type: str,
        alias: str,
        supersedes_alias: str | None = None,
    ) -> PaymentInstrument:
        """Register a new payment instrument, creating supersession chain if needed."""
        old_instrument_node_id = None
        if supersedes_alias:
            old_node = self.adapter.get_instrument_node(supersedes_alias, customer_id)
            if old_node:
                old_instrument_node_id = old_node["id"]

        instrument = PaymentInstrument(
            customer_id=customer_id,
            instrument_type=instrument_type,
            fingerprint_or_safe_alias=alias,
            status="active",
            supersedes_instrument_id=supersedes_alias,
        )

        waggle_node_id = self.adapter.store_payment_instrument(
            instrument=instrument,
            old_instrument_node_id=old_instrument_node_id,
        )
        instrument.waggle_node_id = waggle_node_id

        # Persist in app DB
        instr_dict = instrument.model_dump(mode="json")
        instr_dict["waggle_node_id"] = waggle_node_id
        self.db.upsert_instrument(instr_dict)

        # Mark old instrument as superseded in DB
        if supersedes_alias:
            self.db.execute_write(
                "UPDATE payment_instruments SET status = 'superseded' WHERE customer_id = ? AND fingerprint_or_safe_alias = ?",
                (customer_id, supersedes_alias),
            )

        return instrument
