"""Three baseline systems for comparison in evaluation.

Baseline A: Blind Fixed Retry (floor)
Baseline B: Contextual History Baseline (strong, but no supersession/graph)
System C: Waggle Recover (full temporal graph + supersession)
"""
from __future__ import annotations

import logging

from app.domain.enums import MemoryContribution, RecoveryAction
from app.domain.models import RecoveryDecision
from app.evaluation.generator import EvalScenario

LOGGER = logging.getLogger(__name__)

DEFAULT_RETRY_SECONDS = 480  # 8 minutes


# ── Baseline A: Blind Fixed Retry ──────────────────────────────────────────


class BlindFixedRetryBaseline:
    """
    Baseline A: Always retry same method after a fixed delay.
    Bounded at max 3 attempts.
    This is the floor — represents naive retry logic.
    """

    name = "Baseline A: Blind Fixed Retry"

    def decide(self, scenario: EvalScenario, retry_count: int = 0) -> RecoveryDecision:
        if retry_count >= 3:
            return RecoveryDecision(
                failure_id=scenario.id,
                action=RecoveryAction.STOP,
                confidence=1.0,
                reason="Max attempts reached",
                memory_contribution=MemoryContribution.NONE,
            )

        return RecoveryDecision(
            failure_id=scenario.id,
            action=RecoveryAction.RETRY_AFTER,
            retry_after_seconds=DEFAULT_RETRY_SECONDS,
            recommended_method=scenario.method,
            confidence=0.5,
            reason=f"Blind fixed retry after {DEFAULT_RETRY_SECONDS}s",
            memory_contribution=MemoryContribution.NONE,
        )


# ── Baseline B: Contextual History Baseline ──────────────────────────────


class ContextualHistoryBaseline:
    """
    Baseline B: Uses recent history and simple heuristics.
    Does NOT traverse temporal update chains.
    Does NOT perform supersession validation.
    This is the strong baseline — represents a modern retry strategy.

    IMPORTANT: This is NOT Razorpay's algorithm. It is a transparent
    contextual-history baseline for benchmark purposes only.
    """

    name = "Baseline B: Contextual History"

    def decide(self, scenario: EvalScenario, retry_count: int = 0) -> RecoveryDecision:
        history = scenario.history

        if retry_count >= 3:
            return RecoveryDecision(
                failure_id=scenario.id,
                action=RecoveryAction.STOP,
                confidence=1.0,
                reason="Max attempts reached",
                memory_contribution=MemoryContribution.NONE,
            )

        # Simple heuristic: permanent failures → suggest method
        if scenario.failure_code in ("expired_card", "card_blocked", "do_not_honour"):
            return RecoveryDecision(
                failure_id=scenario.id,
                action=RecoveryAction.SUGGEST_METHOD,
                recommended_method=self._pick_alt(scenario.method),
                confidence=0.70,
                reason="Permanent failure — suggest alternative method",
                memory_contribution=MemoryContribution.PARTIAL,
            )

        # Find recent successful retries in history
        successful_retries = [
            h for h in history
            if h.outcome == "SUCCESS"
            and h.action_taken == "RETRY_AFTER"
            and h.retry_after_seconds
        ]

        if successful_retries:
            # Sort by recency
            successful_retries.sort(key=lambda h: h.timestamp, reverse=True)
            best = successful_retries[0]
            # Use this retry interval — but DOES NOT check if instrument was superseded
            return RecoveryDecision(
                failure_id=scenario.id,
                action=RecoveryAction.RETRY_AFTER,
                retry_after_seconds=best.retry_after_seconds,
                recommended_method=scenario.method,
                confidence=0.70,
                reason=f"Recent successful retry at {best.retry_after_seconds}s",
                memory_contribution=MemoryContribution.PARTIAL,
            )

        # Find successful alternative methods
        success_methods = {
            h.method for h in history
            if h.outcome == "SUCCESS" and h.method != scenario.method
        }
        if success_methods:
            method = next(iter(success_methods))
            return RecoveryDecision(
                failure_id=scenario.id,
                action=RecoveryAction.SUGGEST_METHOD,
                recommended_method=method,
                confidence=0.65,
                reason=f"Historical success with {method}",
                memory_contribution=MemoryContribution.PARTIAL,
            )

        # Transient fallback
        if scenario.failure_code in (
            "issuer_unavailable", "network_error", "gateway_timeout", "route_degraded"
        ):
            return RecoveryDecision(
                failure_id=scenario.id,
                action=RecoveryAction.RETRY_AFTER,
                retry_after_seconds=DEFAULT_RETRY_SECONDS,
                recommended_method=scenario.method,
                confidence=0.50,
                reason="Transient failure — retry after default interval",
                memory_contribution=MemoryContribution.NONE,
            )

        return RecoveryDecision(
            failure_id=scenario.id,
            action=RecoveryAction.CUSTOMER_NUDGE,
            confidence=0.40,
            reason="No useful history — nudge customer",
            memory_contribution=MemoryContribution.NONE,
        )

    def _pick_alt(self, current_method: str) -> str:
        alt_map = {"card": "upi", "upi": "netbanking", "netbanking": "wallet", "wallet": "card"}
        return alt_map.get(current_method, "upi")
