"""Domain models for Waggle Recover — application-level, independent of Waggle Core."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.domain.enums import (
    FailureClass,
    MemoryContribution,
    OutcomeStatus,
    PolicyResult,
    RecoveryAction,
    RetrievalMode,
    TemporalStatus,
    classify_failure,
)


def _new_id() -> str:
    return str(uuid4())


class PaymentFailure(BaseModel):
    id: str = Field(default_factory=_new_id)
    external_payment_id: str
    order_id: str = ""
    customer_id: str
    merchant_id: str
    amount: int  # paise
    currency: str = "INR"
    method: str  # card / upi / netbanking / wallet
    instrument_id: str = ""  # safe alias e.g. card_1234
    route: str = ""
    failure_code: str = ""
    failure_reason: str = ""
    failure_source: str = ""
    failure_step: str = ""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_event_id: str = ""
    recovery_episode_id: str = ""
    failure_class: FailureClass = FailureClass.UNKNOWN

    def model_post_init(self, __context: Any) -> None:
        if self.failure_class == FailureClass.UNKNOWN:
            self.failure_class = classify_failure(self.failure_code, self.failure_reason)

    @property
    def amount_rupees(self) -> float:
        return self.amount / 100.0

    def tag_list(self) -> list[str]:
        tags = [
            "payment_failure",
            f"customer:{self.customer_id}",
            f"merchant:{self.merchant_id}",
            f"method:{self.method}",
        ]
        if self.failure_code:
            tags.append(f"failure_reason:{self.failure_code}")
        if self.instrument_id:
            tags.append(f"instrument:{self.instrument_id}")
        return tags


class PaymentInstrument(BaseModel):
    id: str = Field(default_factory=_new_id)
    customer_id: str
    instrument_type: str  # card / upi / wallet / netbanking
    fingerprint_or_safe_alias: str  # e.g. card_1234
    status: str = "active"  # active / superseded / expired
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    supersedes_instrument_id: str | None = None  # old instrument this replaces
    last_success_at: datetime | None = None

    # Waggle node id (populated after storing in Waggle)
    waggle_node_id: str | None = None

    def tag_list(self) -> list[str]:
        tags = [
            "payment_instrument",
            f"customer:{self.customer_id}",
            f"instrument_type:{self.instrument_type}",
            f"instrument:{self.fingerprint_or_safe_alias}",
        ]
        if self.status:
            tags.append(f"status:{self.status}")
        return tags


class MerchantPolicy(BaseModel):
    policy_id: str = Field(default_factory=_new_id)
    merchant_id: str
    version: int = 1
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    supersedes_policy_id: str | None = None
    max_recovery_attempts: int = 3
    min_retry_interval_seconds: int = 300  # 5 min
    max_retry_interval_seconds: int = 3600  # 1 hour
    allowed_actions: list[RecoveryAction] = Field(
        default_factory=lambda: [
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_AFTER,
            RecoveryAction.SUGGEST_METHOD,
            RecoveryAction.CUSTOMER_NUDGE,
            RecoveryAction.STOP,
        ]
    )
    blocked_methods: list[str] = Field(default_factory=list)
    blocked_routes: list[str] = Field(default_factory=list)
    cooldown_seconds: int = 600
    requires_human_review: bool = False
    requires_human_review_below_confidence: bool = False
    min_automatic_confidence: float = 0.60

    def allows_action(self, action: RecoveryAction) -> bool:
        return action in self.allowed_actions

    def allows_method(self, method: str) -> bool:
        return method not in self.blocked_methods


class EvidenceReference(BaseModel):
    waggle_node_id: str
    label: str
    memory_type: str  # payment_failure / recovery_decision / recovery_outcome / instrument
    relevance_score: float = 0.0
    temporal_status: TemporalStatus = TemporalStatus.UNKNOWN
    accepted: bool = True
    rejection_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Score components (for explainability)
    score_components: dict[str, float] = Field(default_factory=dict)


class StrategyPriorEstimate(BaseModel):
    """Auditable Bayesian estimate for one currently viable recovery strategy."""

    action: RecoveryAction
    recommended_method: str | None = None
    posterior_success_probability: float
    global_prior: float
    weighted_successes: float
    weighted_failures: float
    effective_n: float
    insufficient_history: bool
    selected_bucket: str
    authoritative_evidence_ids: list[str] = Field(default_factory=list)
    excluded_stale_evidence_ids: list[str] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    current_failure: PaymentFailure
    accepted_evidence: list[EvidenceReference] = Field(default_factory=list)
    discarded_evidence: list[EvidenceReference] = Field(default_factory=list)
    merchant_policy: MerchantPolicy | None = None
    retrieval_mode: RetrievalMode = RetrievalMode.FULL_CONTEXT
    retrieval_latency_ms: float = 0.0
    memory_contribution: MemoryContribution = MemoryContribution.NONE
    current_instruments: list[PaymentInstrument] = Field(default_factory=list)
    retry_count: int = 0
    strategy_priors: list[StrategyPriorEstimate] = Field(default_factory=list)


class RecoveryDecision(BaseModel):
    id: str = Field(default_factory=_new_id)
    failure_id: str
    action: RecoveryAction
    retry_after_seconds: int | None = None
    recommended_method: str | None = None
    recommended_route: str | None = None
    confidence: float = 0.5
    evidence_confidence: float = 0.0
    evidence_quality: str = "UNKNOWN"
    uncertainty_reason: str = ""
    abstention_reason: str = ""
    risk_score: int = 0
    risk_band: str = "LOW"
    risk_factors: list[str] = Field(default_factory=list)
    reason: str = ""
    status: str = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Policy outcome
    policy_result: PolicyResult = PolicyResult.ALLOW
    policy_note: str = ""

    # Memory contribution
    memory_contribution: MemoryContribution = MemoryContribution.NONE
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    discarded_evidence: list[EvidenceReference] = Field(default_factory=list)
    strategy_priors: list[StrategyPriorEstimate] = Field(default_factory=list)

    # Human-readable explanation
    explanation: str = ""

    # An escalation is a terminal safety state, not a payment action.  It is
    # deliberately explicit so an operations team can audit exactly why the
    # autonomous workflow stopped and what context must be reviewed.
    human_review_required: bool = False
    escalation_reason: str = ""
    attempt_count: int = 0
    max_automated_attempts: int = 0
    last_safe_action: RecoveryAction | None = None
    recovery_episode_id: str = ""

    # Waggle node id (populated after storing)
    waggle_node_id: str | None = None

    def tag_list(self) -> list[str]:
        tags = [
            "recovery_decision",
            f"action:{self.action.lower()}",
        ]
        if self.recommended_method:
            tags.append(f"method:{self.recommended_method}")
        return tags


class RecoveryAttempt(BaseModel):
    id: str = Field(default_factory=_new_id)
    failure_id: str
    customer_id: str
    merchant_id: str
    action_type: RecoveryAction
    recommended_method: str | None = None
    recommended_route: str | None = None
    retry_after_seconds: int | None = None
    decision_id: str = ""
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    outcome: OutcomeStatus = OutcomeStatus.PENDING
    recovered_amount: int = 0
    failure_reason_if_any: str = ""
    # Provenance copied from the originating failure; required for stale-memory validation.
    method: str = ""
    instrument_id: str = ""
    failure_code: str = ""
    recovery_episode_id: str = ""

    # Waggle node id for outcome
    waggle_outcome_node_id: str | None = None


class NormalizedPaymentEvent(BaseModel):
    """Common event shape from both Razorpay webhooks and simulator."""
    event_type: str  # payment.failed / payment.captured
    payment_id: str
    order_id: str = ""
    customer_id: str
    merchant_id: str
    amount: int  # paise
    currency: str = "INR"
    method: str
    instrument_id: str = ""
    route: str = ""
    error_code: str = ""
    error_description: str = ""
    error_source: str = ""
    error_step: str = ""
    error_reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "razorpay"  # razorpay / simulator
    event_id: str = ""
    test_mode: bool = True
    subscription_id: str = ""
    mandate_id: str = ""
    invoice_id: str = ""


class RevenueRiskEvent(BaseModel):
    """Provider-neutral revenue-risk input normalized into the recovery pipeline."""

    risk_type: str  # PAYMENT_FAILURE / SUBSCRIPTION_FAILURE
    event_id: str
    payment_id: str = ""
    subscription_id: str = ""
    mandate_id: str = ""
    customer_id: str
    merchant_id: str
    amount: int
    currency: str = "INR"
    method: str
    instrument_id: str = ""
    failure_code: str = ""
    failure_reason: str = ""
    next_billing_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    test_mode: bool = True


class RecoveryEpisode(BaseModel):
    """Stable correlation boundary for retry budgets and human review."""

    id: str
    scope_type: str
    scope_id: str
    external_payment_id: str = ""
    order_id: str = ""
    subscription_id: str = ""
    mandate_id: str = ""
    invoice_id: str = ""
    customer_id: str
    merchant_id: str
    status: str = "OPEN"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EscalationRecord(BaseModel):
    """Durable, audit-first handoff; it never represents money movement."""

    id: str = Field(default_factory=_new_id)
    recovery_episode_id: str
    failure_id: str
    decision_id: str
    merchant_id: str
    customer_id: str
    amount: int
    failure_reason: str
    attempts_used: int
    max_automated_attempts: int = 0
    candidate_action: RecoveryAction
    policy_result: PolicyResult
    escalation_reason: str
    accepted_evidence_ids: list[str] = Field(default_factory=list)
    rejected_evidence_ids: list[str] = Field(default_factory=list)
    recommended_manual_next_step: str = "Manual review / customer outreach"
    state: str = "PENDING"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    waggle_node_id: str | None = None


class MandateContext(BaseModel):
    """Context for mandate (recurring payment) recovery."""
    mandate_id: str
    customer_id: str
    merchant_id: str
    amount: int
    currency: str = "INR"
    instrument_id: str = ""
    cycle_number: int = 1
    previous_failures: int = 0
    last_attempt_at: datetime | None = None
    next_scheduled_at: datetime | None = None
    allowed_actions: list[RecoveryAction] = Field(
        default_factory=lambda: [
            RecoveryAction.CUSTOMER_NUDGE,
            RecoveryAction.WAIT_NEXT_CYCLE,
            RecoveryAction.SUGGEST_METHOD,
            RecoveryAction.ESCALATE,
            RecoveryAction.STOP,
        ]
    )
