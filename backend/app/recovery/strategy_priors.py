"""Authoritative, recency-weighted Bayesian recovery strategy priors."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.domain.enums import FailureClass, OutcomeStatus, RecoveryAction, classify_failure
from app.domain.models import EvidenceBundle, StrategyPriorEstimate
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter


@dataclass(frozen=True)
class StrategyCandidate:
    action: RecoveryAction
    recommended_method: str | None = None


@dataclass(frozen=True)
class AuthoritativeOutcome:
    node_id: str
    merchant_id: str
    customer_id: str
    action: RecoveryAction
    recommended_method: str | None
    outcome: OutcomeStatus
    method: str
    instrument_id: str
    failure_code: str
    failure_class: FailureClass
    executed_at: datetime


ALTERNATIVE_METHOD_ORDER = ("upi", "netbanking", "wallet", "card", "emi", "paylater")


def viable_strategy_candidates(bundle: EvidenceBundle) -> list[StrategyCandidate]:
    """Return only strategies allowed by hard/domain constraints before ranking."""
    failure = bundle.current_failure
    policy = bundle.merchant_policy
    allowed = set(policy.allowed_actions) if policy else {
        RecoveryAction.RETRY_AFTER,
        RecoveryAction.SUGGEST_METHOD,
        RecoveryAction.CUSTOMER_NUDGE,
        RecoveryAction.STOP,
    }

    # Preserve the existing deterministic friction stop before any adaptive ranking.
    if bundle.retry_count >= 2:
        return [StrategyCandidate(RecoveryAction.STOP)]

    blocked_methods = set(policy.blocked_methods if policy else [])
    alternative = next(
        (
            method
            for method in ALTERNATIVE_METHOD_ORDER
            if method != failure.method and method not in blocked_methods
        ),
        None,
    )
    candidates: list[StrategyCandidate] = []

    if failure.failure_class in (FailureClass.PERMANENT, FailureClass.INSTRUMENT):
        if RecoveryAction.SUGGEST_METHOD in allowed and alternative:
            candidates.append(StrategyCandidate(RecoveryAction.SUGGEST_METHOD, alternative))
        if RecoveryAction.CUSTOMER_NUDGE in allowed:
            candidates.append(StrategyCandidate(RecoveryAction.CUSTOMER_NUDGE))
    elif failure.failure_class == FailureClass.TRANSIENT:
        if RecoveryAction.RETRY_AFTER in allowed:
            candidates.append(StrategyCandidate(RecoveryAction.RETRY_AFTER, failure.method))
        if RecoveryAction.SUGGEST_METHOD in allowed and alternative:
            candidates.append(StrategyCandidate(RecoveryAction.SUGGEST_METHOD, alternative))
        if RecoveryAction.CUSTOMER_NUDGE in allowed:
            candidates.append(StrategyCandidate(RecoveryAction.CUSTOMER_NUDGE))
    elif RecoveryAction.CUSTOMER_NUDGE in allowed:
        candidates.append(StrategyCandidate(RecoveryAction.CUSTOMER_NUDGE))

    if not candidates:
        candidates.append(StrategyCandidate(RecoveryAction.STOP))
    return candidates


def get_strategy_priors(
    bundle: EvidenceBundle,
    adapter: WaggleRecoveryMemoryAdapter,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[StrategyPriorEstimate]:
    """Estimate viable strategy performance from validated Waggle outcomes only."""
    current_time = now or datetime.now(UTC)
    candidates = [candidate for candidate in viable_strategy_candidates(bundle) if candidate.action != RecoveryAction.STOP]
    if not candidates:
        return []

    observations, excluded_stale_ids = _load_authoritative_outcomes(
        adapter,
        now=current_time,
        max_per_action=settings.max_evidence_nodes,
    )
    half_life_days = settings.evidence_recency_half_life_days
    decay_lambda = math.log(2) / half_life_days
    estimates: list[StrategyPriorEstimate] = []

    for candidate in candidates:
        action_observations = [item for item in observations if item.action == candidate.action]
        global_pool = [item for item in action_observations if item.merchant_id != bundle.current_failure.merchant_id]
        global_levels = [
            (
                "global_failure_class_method_strategy",
                [
                    item
                    for item in global_pool
                    if item.failure_class == bundle.current_failure.failure_class
                    and item.method == bundle.current_failure.method
                    and _method_matches(item, candidate)
                ],
            ),
            (
                "global_failure_class_strategy",
                [item for item in global_pool if item.failure_class == bundle.current_failure.failure_class],
            ),
            ("global_action", global_pool),
        ]
        _, selected_global = next(((name, items) for name, items in global_levels if items), ("default_beta_prior", []))
        global_successes, global_trials = _weighted_counts(selected_global, current_time, decay_lambda)
        global_prior = global_successes / global_trials if global_trials else 0.5

        merchant_pool = [
            item for item in action_observations if item.merchant_id == bundle.current_failure.merchant_id
        ]
        merchant_levels = [
            (
                "merchant_failure_code_method_strategy",
                [
                    item
                    for item in merchant_pool
                    if item.failure_code == bundle.current_failure.failure_code
                    and item.method == bundle.current_failure.method
                    and _method_matches(item, candidate)
                ],
            ),
            (
                "merchant_failure_class_strategy",
                [
                    item
                    for item in merchant_pool
                    if item.failure_class == bundle.current_failure.failure_class and _method_matches(item, candidate)
                ],
            ),
            (
                "merchant_failure_class_action",
                [item for item in merchant_pool if item.failure_class == bundle.current_failure.failure_class],
            ),
        ]
        bucket_name, selected_merchant = next(
            ((name, items) for name, items in merchant_levels if items),
            ("global_prior_only", []),
        )
        weighted_successes, effective_n = _weighted_counts(selected_merchant, current_time, decay_lambda)
        weighted_failures = effective_n - weighted_successes
        posterior = (
            weighted_successes + settings.strategy_prior_kappa * global_prior
        ) / (effective_n + settings.strategy_prior_kappa)

        estimates.append(StrategyPriorEstimate(
            action=candidate.action,
            recommended_method=candidate.recommended_method,
            posterior_success_probability=round(posterior, 6),
            global_prior=round(global_prior, 6),
            weighted_successes=round(weighted_successes, 6),
            weighted_failures=round(weighted_failures, 6),
            effective_n=round(effective_n, 6),
            insufficient_history=effective_n < settings.strategy_min_effective_n,
            selected_bucket=bucket_name,
            authoritative_evidence_ids=[item.node_id for item in selected_merchant],
            excluded_stale_evidence_ids=excluded_stale_ids,
        ))

    return sorted(estimates, key=lambda item: (-item.posterior_success_probability, item.action.value))


def _method_matches(observation: AuthoritativeOutcome, candidate: StrategyCandidate) -> bool:
    if candidate.action != RecoveryAction.SUGGEST_METHOD:
        return True
    return observation.recommended_method == candidate.recommended_method


def _weighted_counts(
    observations: list[AuthoritativeOutcome],
    now: datetime,
    decay_lambda: float,
) -> tuple[float, float]:
    successes = 0.0
    trials = 0.0
    for observation in observations:
        age_days = max(0.0, (now - observation.executed_at).total_seconds() / 86_400)
        weight = math.exp(-decay_lambda * age_days)
        trials += weight
        if observation.outcome == OutcomeStatus.SUCCESS:
            successes += weight
    return successes, trials


def _load_authoritative_outcomes(
    adapter: WaggleRecoveryMemoryAdapter,
    *,
    now: datetime,
    max_per_action: int,
) -> tuple[list[AuthoritativeOutcome], list[str]]:
    """Read bounded outcome history and explicitly veto superseded instruments."""
    snapshot = adapter.graph.get_graph_snapshot()
    nodes = snapshot.get("nodes", [])
    edges = snapshot.get("edges", [])
    instrument_nodes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for node in nodes:
        if "payment_instrument" not in (node.get("tags") or []):
            continue
        metadata = node.get("metadata") or {}
        key = (str(metadata.get("customer_id") or ""), str(metadata.get("alias") or ""))
        instrument_nodes.setdefault(key, []).append(node)

    updated_targets = {
        str(edge.get("target_id"))
        for edge in edges
        if str(edge.get("relationship", "")).lower().endswith("updates")
    }
    eligible: list[AuthoritativeOutcome] = []
    excluded_stale: list[str] = []
    for node in nodes:
        if "recovery_outcome" not in (node.get("tags") or []):
            continue
        metadata = node.get("metadata") or {}
        try:
            action = RecoveryAction(str(metadata.get("action_type") or ""))
            outcome = OutcomeStatus(str(metadata.get("outcome") or ""))
        except ValueError:
            continue
        if outcome not in (OutcomeStatus.SUCCESS, OutcomeStatus.FAILURE):
            continue

        customer_id = str(metadata.get("customer_id") or "")
        instrument_id = str(metadata.get("instrument_id") or "")
        related_instruments = instrument_nodes.get((customer_id, instrument_id), []) if instrument_id else []
        node_is_expired = _is_expired(node.get("valid_to"), now)
        instrument_is_superseded = any(
            instrument.get("id") in updated_targets
            or _is_expired(instrument.get("valid_to"), now)
            or str((instrument.get("metadata") or {}).get("status", "")).lower() == "superseded"
            for instrument in related_instruments
        )
        if node_is_expired or instrument_is_superseded:
            excluded_stale.append(str(node.get("id")))
            continue

        executed_at = _parse_datetime(metadata.get("executed_at") or node.get("valid_from") or node.get("created_at"))
        eligible.append(AuthoritativeOutcome(
            node_id=str(node.get("id")),
            merchant_id=str(metadata.get("merchant_id") or ""),
            customer_id=customer_id,
            action=action,
            recommended_method=(str(metadata.get("recommended_method")) if metadata.get("recommended_method") else None),
            outcome=outcome,
            method=str(metadata.get("method") or ""),
            instrument_id=instrument_id,
            failure_code=str(metadata.get("failure_code") or ""),
            failure_class=classify_failure(str(metadata.get("failure_code") or "")),
            executed_at=executed_at,
        ))

    eligible.sort(key=lambda item: item.executed_at, reverse=True)
    bounded: list[AuthoritativeOutcome] = []
    action_counts: dict[RecoveryAction, int] = {}
    for item in eligible:
        count = action_counts.get(item.action, 0)
        if count >= max_per_action:
            continue
        action_counts[item.action] = count + 1
        bounded.append(item)
    return bounded, sorted(set(excluded_stale))


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_expired(value: Any, now: datetime) -> bool:
    if not value:
        return False
    return _parse_datetime(value) < now
