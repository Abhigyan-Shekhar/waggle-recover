"""Policy engine — validates and potentially modifies candidate decisions.

Acts as a safety layer between decision providers and execution.
No LLM or heuristic can bypass this.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.domain.enums import PolicyResult, RecoveryAction
from app.domain.models import EvidenceBundle, MerchantPolicy, RecoveryDecision

LOGGER = logging.getLogger(__name__)


@dataclass
class PolicyCheck:
    check_name: str
    passed: bool
    note: str = ""


@dataclass
class PolicyValidationResult:
    result: PolicyResult
    checks: list[PolicyCheck]
    modified_action: RecoveryAction | None = None
    modified_retry_seconds: int | None = None
    block_reason: str = ""
    notes: list[str] = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []

    def formatted(self) -> str:
        lines = [f"Policy result: {self.result}"]
        for check in self.checks:
            icon = "✓" if check.passed else "✗"
            lines.append(f"  {icon} {check.check_name}: {check.note}")
        if self.block_reason:
            lines.append(f"  Block reason: {self.block_reason}")
        return "\n".join(lines)


class PolicyEngine:
    """
    Validates candidate recovery decisions against merchant policy.

    Results:
    - ALLOW: proceed as-is
    - MODIFY: adjust action/timing to comply with policy
    - BLOCK: action not allowed; substitute STOP
    """

    def validate(
        self,
        decision: RecoveryDecision,
        bundle: EvidenceBundle,
        retry_count: int = 0,
        merchant_policy: MerchantPolicy | None = None,
    ) -> PolicyValidationResult:
        policy = merchant_policy or bundle.merchant_policy
        if policy is None:
            # No policy — allow with minimal constraint
            return PolicyValidationResult(
                result=PolicyResult.ALLOW,
                checks=[PolicyCheck("policy_exists", False, "No merchant policy — using defaults")],
            )

        checks: list[PolicyCheck] = []
        block_reason = ""
        modifications: dict = {}

        # 1. Check max recovery attempts
        max_attempts_check = self._check_max_attempts(retry_count, policy)
        checks.append(max_attempts_check)
        if not max_attempts_check.passed:
            block_reason = max_attempts_check.note
            return PolicyValidationResult(
                result=PolicyResult.BLOCK,
                checks=checks,
                block_reason=block_reason,
            )

        # 2. Check action is allowed
        action_check = self._check_action_allowed(decision.action, policy)
        checks.append(action_check)
        if not action_check.passed:
            # Try to find an allowed alternative
            alt = self._find_allowed_alternative(decision.action, policy)
            if alt:
                modifications["action"] = alt
                checks.append(PolicyCheck("action_substitution", True, f"Substituted {alt}"))
            else:
                block_reason = f"Action {decision.action} not in allowed set and no substitute available"
                return PolicyValidationResult(
                    result=PolicyResult.BLOCK,
                    checks=checks,
                    block_reason=block_reason,
                )

        # 3. Check method not blocked
        if decision.recommended_method:
            method_check = self._check_method_allowed(decision.recommended_method, policy)
            checks.append(method_check)
            if not method_check.passed:
                block_reason = f"Recommended method {decision.recommended_method} is blocked"
                return PolicyValidationResult(
                    result=PolicyResult.BLOCK,
                    checks=checks,
                    block_reason=block_reason,
                )

        # 3b. Routes are a separate merchant constraint.
        if decision.recommended_route:
            route_check = PolicyCheck(
                "route_allowed",
                decision.recommended_route not in policy.blocked_routes,
                f"{decision.recommended_route} {'not blocked' if decision.recommended_route not in policy.blocked_routes else 'is blocked'}",
            )
            checks.append(route_check)
            if not route_check.passed:
                return PolicyValidationResult(result=PolicyResult.BLOCK, checks=checks,
                                              block_reason=f"Recommended route {decision.recommended_route} is blocked")

        # 4. Check retry interval bounds
        if decision.action in (RecoveryAction.RETRY_AFTER, RecoveryAction.RETRY_NOW):
            if decision.action == RecoveryAction.RETRY_NOW:
                modifications["action"] = RecoveryAction.RETRY_AFTER
                modifications["retry_after_seconds"] = policy.min_retry_interval_seconds
                checks.append(PolicyCheck("retry_now_interval", False,
                    f"RETRY_NOW converted to RETRY_AFTER({policy.min_retry_interval_seconds}s)"))
            interval_result, modified_seconds = self._check_retry_interval(decision.retry_after_seconds, policy)
            checks.append(interval_result)
            if not interval_result.passed and modified_seconds is not None:
                modifications["retry_after_seconds"] = modified_seconds

        # 5. Check for permanent failure — don't retry same method
        permanent_check = self._check_not_permanent_retry(decision, bundle)
        checks.append(permanent_check)
        if not permanent_check.passed:
            # Suggest method change instead
            if RecoveryAction.SUGGEST_METHOD in policy.allowed_actions:
                modifications["action"] = RecoveryAction.SUGGEST_METHOD
                modifications["retry_after_seconds"] = None
                checks.append(PolicyCheck("permanent_retry_blocked", True, "Changed to SUGGEST_METHOD"))
            else:
                modifications["action"] = RecoveryAction.STOP
                checks.append(PolicyCheck("permanent_retry_blocked", True, "Changed to STOP"))

        # 6. Already at maximum — force STOP
        if retry_count >= policy.max_recovery_attempts:
            checks.append(PolicyCheck("stop_rule", True, f"Max attempts {policy.max_recovery_attempts} reached"))
            return PolicyValidationResult(
                result=PolicyResult.BLOCK,
                checks=checks,
                modified_action=RecoveryAction.STOP,
                block_reason=f"Maximum recovery attempts ({policy.max_recovery_attempts}) reached",
            )

        # Determine final result
        if modifications:
            return PolicyValidationResult(
                result=PolicyResult.MODIFY,
                checks=checks,
                modified_action=modifications.get("action"),
                modified_retry_seconds=modifications.get("retry_after_seconds"),
            )

        return PolicyValidationResult(
            result=PolicyResult.ALLOW,
            checks=checks,
        )

    def _check_max_attempts(self, retry_count: int, policy: MerchantPolicy) -> PolicyCheck:
        if retry_count >= policy.max_recovery_attempts:
            return PolicyCheck(
                "max_attempts",
                False,
                f"retry_count={retry_count} >= max={policy.max_recovery_attempts}",
            )
        return PolicyCheck(
            "max_attempts",
            True,
            f"attempt count {retry_count}/{policy.max_recovery_attempts} within limit",
        )

    def _check_action_allowed(self, action: RecoveryAction, policy: MerchantPolicy) -> PolicyCheck:
        if policy.allows_action(action):
            return PolicyCheck("action_allowed", True, f"{action} is in allowed set")
        return PolicyCheck("action_allowed", False, f"{action} not in allowed set {[a.value for a in policy.allowed_actions]}")

    def _check_method_allowed(self, method: str, policy: MerchantPolicy) -> PolicyCheck:
        if policy.allows_method(method):
            return PolicyCheck("method_allowed", True, f"{method} not blocked")
        return PolicyCheck("method_allowed", False, f"{method} in blocked_methods")

    def _check_retry_interval(
        self, retry_seconds: int | None, policy: MerchantPolicy
    ) -> tuple[PolicyCheck, int | None]:
        if retry_seconds is None:
            return PolicyCheck("retry_interval", False, f"missing interval → adjusted to {policy.min_retry_interval_seconds}s"), policy.min_retry_interval_seconds

        if retry_seconds < policy.min_retry_interval_seconds:
            adj = policy.min_retry_interval_seconds
            return (
                PolicyCheck(
                    "retry_interval",
                    False,
                    f"{retry_seconds}s < min {policy.min_retry_interval_seconds}s → adjusted to {adj}s",
                ),
                adj,
            )

        if retry_seconds > policy.max_retry_interval_seconds:
            adj = policy.max_retry_interval_seconds
            return (
                PolicyCheck(
                    "retry_interval",
                    False,
                    f"{retry_seconds}s > max {policy.max_retry_interval_seconds}s → adjusted to {adj}s",
                ),
                adj,
            )

        return PolicyCheck("retry_interval", True, f"{retry_seconds}s within [{policy.min_retry_interval_seconds}, {policy.max_retry_interval_seconds}]s"), None

    def _check_not_permanent_retry(
        self, decision: RecoveryDecision, bundle: EvidenceBundle
    ) -> PolicyCheck:
        from app.domain.enums import FailureClass
        failure = bundle.current_failure
        is_permanent = failure.failure_class in (FailureClass.PERMANENT, FailureClass.INSTRUMENT)
        is_retry = decision.action in (RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_AFTER)

        if is_permanent and is_retry:
            return PolicyCheck(
                "not_permanent_retry",
                False,
                f"Permanent failure class {failure.failure_class} should not retry same method",
            )
        return PolicyCheck(
            "not_permanent_retry",
            True,
            "not a permanent failure or not a retry action",
        )

    def _find_allowed_alternative(
        self, blocked_action: RecoveryAction, policy: MerchantPolicy
    ) -> RecoveryAction | None:
        # Priority order for fallback actions
        fallback_order = [
            RecoveryAction.SUGGEST_METHOD,
            RecoveryAction.CUSTOMER_NUDGE,
            RecoveryAction.RETRY_AFTER,
            RecoveryAction.STOP,
        ]
        for action in fallback_order:
            if action != blocked_action and policy.allows_action(action):
                return action
        return None
