"""Tests for authoritative recency-weighted Bayesian strategy priors."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph

from app.config import Settings
from app.domain.enums import FailureClass, OutcomeStatus, RecoveryAction
from app.domain.models import (
    EvidenceBundle,
    MerchantPolicy,
    PaymentFailure,
    PaymentInstrument,
    RecoveryAttempt,
    StrategyPriorEstimate,
)
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.recovery.decision_engine import DeterministicDecisionProvider
from app.recovery.strategy_priors import get_strategy_priors, viable_strategy_candidates

NOW = datetime(2026, 1, 31, tzinfo=UTC)


@pytest.fixture
def adapter(tmp_path) -> WaggleRecoveryMemoryAdapter:
    graph = MemoryGraph(
        db_path=str(tmp_path / "priors.db"),
        embedding_model=EmbeddingModel("fake"),
    )
    return WaggleRecoveryMemoryAdapter(graph.for_tenant("strategy-priors"))


def bundle(merchant_id: str = "MERCH-TARGET") -> EvidenceBundle:
    return EvidenceBundle(
        current_failure=PaymentFailure(
            external_payment_id="pay_current",
            customer_id="CUST-TARGET",
            merchant_id=merchant_id,
            amount=100_000,
            method="card",
            instrument_id="card_target",
            failure_code="issuer_unavailable",
            occurred_at=NOW,
        ),
        merchant_policy=MerchantPolicy(merchant_id=merchant_id),
    )


def store_outcome(
    adapter: WaggleRecoveryMemoryAdapter,
    *,
    merchant_id: str,
    customer_id: str,
    instrument_id: str,
    action: RecoveryAction,
    outcome: OutcomeStatus,
    age_days: float = 0,
    recommended_method: str | None = None,
) -> str:
    if adapter.get_instrument_node(instrument_id, customer_id) is None:
        adapter.store_payment_instrument(PaymentInstrument(
            customer_id=customer_id,
            instrument_type="card",
            fingerprint_or_safe_alias=instrument_id,
            created_at=NOW - timedelta(days=age_days + 1),
        ))
    attempt = RecoveryAttempt(
        failure_id=f"failure-{customer_id}-{action.value}-{age_days}",
        customer_id=customer_id,
        merchant_id=merchant_id,
        action_type=action,
        recommended_method=recommended_method,
        retry_after_seconds=480 if action == RecoveryAction.RETRY_AFTER else None,
        executed_at=NOW - timedelta(days=age_days),
        outcome=outcome,
        recovered_amount=100_000 if outcome == OutcomeStatus.SUCCESS else 0,
        method="card",
        instrument_id=instrument_id,
        failure_code="issuer_unavailable",
    )
    return adapter.store_recovery_outcome(attempt)


def prior_for(priors, action: RecoveryAction):
    return next(item for item in priors if item.action == action)


def test_defaults_are_preregistered():
    settings = Settings()
    assert settings.strategy_prior_kappa == 5.0
    assert settings.strategy_min_effective_n == 5.0
    assert settings.evidence_recency_half_life_days == 14.0


def test_zero_history_uses_neutral_global_prior_without_nan(adapter):
    estimate = prior_for(get_strategy_priors(bundle(), adapter, Settings(), now=NOW), RecoveryAction.RETRY_AFTER)
    assert estimate.global_prior == 0.5
    assert estimate.posterior_success_probability == 0.5
    assert estimate.effective_n == 0
    assert estimate.insufficient_history is True
    assert not math.isnan(estimate.posterior_success_probability)


def test_authoritative_outcome_contributes_and_half_life_is_half_weight(adapter):
    evidence_id = store_outcome(
        adapter,
        merchant_id="MERCH-TARGET",
        customer_id="CUST-HALF-LIFE",
        instrument_id="card_half_life",
        action=RecoveryAction.RETRY_AFTER,
        outcome=OutcomeStatus.SUCCESS,
        age_days=14,
    )
    estimate = prior_for(get_strategy_priors(bundle(), adapter, Settings(), now=NOW), RecoveryAction.RETRY_AFTER)
    assert estimate.weighted_successes == pytest.approx(0.5, abs=1e-6)
    assert estimate.effective_n == pytest.approx(0.5, abs=1e-6)
    assert evidence_id in estimate.authoritative_evidence_ids


def test_superseded_outcome_has_zero_effective_weight(adapter):
    customer_id = "CUST-STALE-PRIOR"
    old = PaymentInstrument(
        customer_id=customer_id,
        instrument_type="card",
        fingerprint_or_safe_alias="card_old_prior",
        created_at=NOW - timedelta(days=10),
    )
    old_node_id = adapter.store_payment_instrument(old)
    stale_outcome_id = store_outcome(
        adapter,
        merchant_id="MERCH-TARGET",
        customer_id=customer_id,
        instrument_id="card_old_prior",
        action=RecoveryAction.RETRY_AFTER,
        outcome=OutcomeStatus.SUCCESS,
    )
    adapter.store_payment_instrument(
        PaymentInstrument(
            customer_id=customer_id,
            instrument_type="card",
            fingerprint_or_safe_alias="card_new_prior",
            supersedes_instrument_id="card_old_prior",
            created_at=NOW - timedelta(days=1),
        ),
        old_instrument_node_id=old_node_id,
    )

    estimate = prior_for(get_strategy_priors(bundle(), adapter, Settings(), now=NOW), RecoveryAction.RETRY_AFTER)
    assert estimate.effective_n == 0
    assert stale_outcome_id not in estimate.authoritative_evidence_ids
    assert stale_outcome_id in estimate.excluded_stale_evidence_ids


def test_sparse_history_shrinks_and_more_history_increases_merchant_influence(adapter):
    for index in range(4):
        store_outcome(
            adapter,
            merchant_id=f"MERCH-GLOBAL-{index}",
            customer_id=f"CUST-GLOBAL-{index}",
            instrument_id=f"card_global_{index}",
            action=RecoveryAction.RETRY_AFTER,
            outcome=OutcomeStatus.FAILURE,
        )
    store_outcome(
        adapter,
        merchant_id="MERCH-TARGET",
        customer_id="CUST-SPARSE",
        instrument_id="card_sparse",
        action=RecoveryAction.RETRY_AFTER,
        outcome=OutcomeStatus.SUCCESS,
    )
    sparse = prior_for(get_strategy_priors(bundle(), adapter, Settings(), now=NOW), RecoveryAction.RETRY_AFTER)
    assert sparse.global_prior == 0
    assert sparse.posterior_success_probability == pytest.approx(1 / 6, abs=1e-6)
    assert sparse.insufficient_history is True

    for index in range(5):
        store_outcome(
            adapter,
            merchant_id="MERCH-TARGET",
            customer_id=f"CUST-WARM-{index}",
            instrument_id=f"card_warm_{index}",
            action=RecoveryAction.RETRY_AFTER,
            outcome=OutcomeStatus.SUCCESS,
        )
    warm = prior_for(get_strategy_priors(bundle(), adapter, Settings(), now=NOW), RecoveryAction.RETRY_AFTER)
    assert warm.effective_n >= 5.0
    assert warm.posterior_success_probability > sparse.posterior_success_probability
    assert warm.insufficient_history is False


def test_unsafe_retry_is_not_a_viable_candidate_for_permanent_failure():
    current = bundle()
    current.current_failure.failure_code = "expired_card"
    current.current_failure.failure_class = FailureClass.PERMANENT
    actions = {candidate.action for candidate in viable_strategy_candidates(current)}
    assert RecoveryAction.RETRY_AFTER not in actions
    assert RecoveryAction.SUGGEST_METHOD in actions


def test_adaptive_ranking_changes_only_safe_ambiguous_decision():
    current = bundle()
    current.strategy_priors = [
        StrategyPriorEstimate(
            action=RecoveryAction.SUGGEST_METHOD,
            recommended_method="upi",
            posterior_success_probability=0.78,
            global_prior=0.5,
            weighted_successes=8,
            weighted_failures=2,
            effective_n=10,
            insufficient_history=False,
            selected_bucket="merchant_failure_code_method_strategy",
        ),
        StrategyPriorEstimate(
            action=RecoveryAction.RETRY_AFTER,
            recommended_method="card",
            posterior_success_probability=0.51,
            global_prior=0.5,
            weighted_successes=5,
            weighted_failures=5,
            effective_n=10,
            insufficient_history=False,
            selected_bucket="merchant_failure_code_method_strategy",
        ),
    ]

    adaptive = DeterministicDecisionProvider().decide(current)
    static = DeterministicDecisionProvider(enable_strategy_priors=False).decide(current)

    assert adaptive.action == RecoveryAction.SUGGEST_METHOD
    assert adaptive.recommended_method == "upi"
    assert "posterior recovery probability 0.78" in adaptive.reason
    assert static.action == RecoveryAction.RETRY_AFTER
    assert static.retry_after_seconds == 480
    repeated = DeterministicDecisionProvider().decide(current)
    assert (
        repeated.action,
        repeated.recommended_method,
        repeated.retry_after_seconds,
        repeated.reason,
    ) == (
        adaptive.action,
        adaptive.recommended_method,
        adaptive.retry_after_seconds,
        adaptive.reason,
    )


def test_high_retry_prior_cannot_override_permanent_failure():
    current = bundle()
    current.current_failure.failure_code = "expired_card"
    current.current_failure.failure_class = FailureClass.PERMANENT
    current.strategy_priors = [
        StrategyPriorEstimate(
            action=RecoveryAction.RETRY_AFTER,
            posterior_success_probability=0.99,
            global_prior=0.5,
            weighted_successes=20,
            weighted_failures=0,
            effective_n=20,
            insufficient_history=False,
            selected_bucket="merchant_failure_code_method_strategy",
        )
    ]

    assert DeterministicDecisionProvider().decide(current).action == RecoveryAction.SUGGEST_METHOD
