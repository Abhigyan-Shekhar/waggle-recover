"""Outcome executor/simulator — simulates payment outcomes for scenarios."""
from __future__ import annotations

import logging
import random
from typing import Any

from app.domain.enums import OutcomeStatus, RecoveryAction
from app.domain.models import MerchantPolicy, RecoveryAttempt, RecoveryDecision

LOGGER = logging.getLogger(__name__)


class RecoveryExecutor:
    """
    Executes or simulates a recovery decision.

    In demo/test mode: uses deterministic outcome simulation.
    In production: would integrate with Razorpay payment retry APIs.
    """

    def execute(
        self,
        decision: RecoveryDecision,
        customer_id: str,
        merchant_id: str,
        failure_id: str,
        original_amount: int,
        method: str = "",
        instrument_id: str = "",
        failure_code: str = "",
        recovery_episode_id: str = "",
        simulate: bool = True,
        simulation_outcomes: dict[str, Any] | None = None,
    ) -> RecoveryAttempt:
        """
        Execute a recovery action and return the attempt record.

        Args:
            decision: The validated recovery decision
            simulate: If True, simulate outcome without real payment
            simulation_outcomes: Dict mapping action → outcome for deterministic evaluation
        """
        from datetime import datetime

        attempt = RecoveryAttempt(
            failure_id=failure_id,
            customer_id=customer_id,
            merchant_id=merchant_id,
            action_type=decision.action,
            recommended_method=decision.recommended_method,
            retry_after_seconds=decision.retry_after_seconds,
            decision_id=decision.id,
            method=method,
            instrument_id=instrument_id,
            failure_code=failure_code,
            recovery_episode_id=recovery_episode_id,
        )

        if simulate:
            outcome, recovered_amount = self._simulate_outcome(
                decision=decision,
                original_amount=original_amount,
                simulation_outcomes=simulation_outcomes,
            )
        else:
            # STOP and ESCALATE are terminal safety states. Neither can move
            # money; a real capture can only close an actually pending action.
            if decision.action in (RecoveryAction.STOP, RecoveryAction.ESCALATE):
                outcome = OutcomeStatus.SKIPPED
                recovered_amount = 0
            else:
                # A real webhook only creates a recommendation. Recovery is
                # confirmed by a later payment.captured event, never guessed.
                outcome, recovered_amount = OutcomeStatus.PENDING, 0

        attempt.outcome = outcome
        attempt.recovered_amount = recovered_amount if outcome == OutcomeStatus.SUCCESS else 0

        LOGGER.info(
            "Recovery attempt: action=%s, outcome=%s, recovered=₹%.2f",
            decision.action,
            outcome,
            attempt.recovered_amount / 100.0,
        )
        return attempt

    def _simulate_outcome(
        self,
        decision: RecoveryDecision,
        original_amount: int,
        simulation_outcomes: dict[str, Any] | None = None,
    ) -> tuple[OutcomeStatus, int]:
        """Deterministic outcome simulation."""
        if simulation_outcomes:
            # Prefer parameter-specific ground truth (e.g. RETRY_AFTER:480 or
            # SUGGEST_METHOD:upi), then fall back to action-level fixtures.
            action_key = decision.action.value
            parameter_key = action_key
            if decision.action == RecoveryAction.RETRY_AFTER and decision.retry_after_seconds is not None:
                parameter_key = f"{action_key}:{decision.retry_after_seconds}"
            elif decision.action == RecoveryAction.SUGGEST_METHOD and decision.recommended_method:
                parameter_key = f"{action_key}:{decision.recommended_method}"
            result = simulation_outcomes.get(parameter_key, simulation_outcomes.get(action_key))
            if isinstance(result, dict):
                result = result.get("outcome", result.get("status"))
            if result is not None:
                if result in ("SUCCESS", OutcomeStatus.SUCCESS, True):
                    return OutcomeStatus.SUCCESS, original_amount
                elif result in ("FAILURE", OutcomeStatus.FAILURE, False):
                    return OutcomeStatus.FAILURE, 0
                elif result in ("SKIPPED", OutcomeStatus.SKIPPED):
                    return OutcomeStatus.SKIPPED, 0

        # Default probabilistic simulation
        if decision.action in (RecoveryAction.STOP, RecoveryAction.ESCALATE):
            return OutcomeStatus.SKIPPED, 0

        if decision.action == RecoveryAction.CUSTOMER_NUDGE:
            # 40% success rate for nudges
            if random.random() < 0.40:
                return OutcomeStatus.SUCCESS, original_amount
            return OutcomeStatus.FAILURE, 0

        if decision.action in (RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_AFTER):
            # Transient failures: 65% success, Permanent: 10%
            success_rate = 0.65 if decision.confidence > 0.7 else 0.45
            if random.random() < success_rate:
                return OutcomeStatus.SUCCESS, original_amount
            return OutcomeStatus.FAILURE, 0

        if decision.action == RecoveryAction.SUGGEST_METHOD:
            # Method suggestions: 70% success rate
            if random.random() < 0.70:
                return OutcomeStatus.SUCCESS, original_amount
            return OutcomeStatus.FAILURE, 0

        return OutcomeStatus.PENDING, 0
