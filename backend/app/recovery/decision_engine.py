"""Decision engine — two-stage: candidate decision + policy validation.

Stage 1: DeterministicDecisionProvider or LLMDecisionProvider → candidate action
Stage 2: PolicyEngine validates and potentially modifies the action
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.domain.enums import FailureClass, MemoryContribution, RecoveryAction
from app.domain.models import EvidenceBundle, MerchantPolicy, RecoveryDecision

LOGGER = logging.getLogger(__name__)

# Transient failures that are safe to retry after a delay
SAFE_TO_RETRY_CODES = {
    "issuer_unavailable",
    "network_error",
    "gateway_timeout",
    "upstream_timeout",
    "server_error",
    "route_degraded",
}

DEFAULT_RETRY_SECONDS = 480  # 8 minutes fallback


class DecisionProvider(ABC):
    @abstractmethod
    def decide(self, bundle: EvidenceBundle) -> RecoveryDecision:
        """Produce a candidate recovery decision from the evidence bundle."""


class DeterministicDecisionProvider(DecisionProvider):
    """
    Rule-based deterministic decision provider.

    Decision strategies implemented:
    1. Timing memory: reuse successful retry interval from accepted evidence
    2. Permanent/instrument failure: suggest method switch
    3. Superseded historical method: fall back to safe method suggestion
    4. Repeated failures: escalate or stop
    5. No memory: safe contextual fallback
    """

    def decide(self, bundle: EvidenceBundle) -> RecoveryDecision:
        failure = bundle.current_failure

        # Strategy 1: Check if we have good timing evidence (same customer + transient + success)
        timing_evidence = self._find_timing_evidence(bundle)
        if timing_evidence is not None:
            retry_seconds, evidence_refs = timing_evidence
            return RecoveryDecision(
                failure_id=failure.id,
                action=RecoveryAction.RETRY_AFTER,
                retry_after_seconds=retry_seconds,
                recommended_method=failure.method,
                confidence=0.82,
                reason=(
                    f"Historical successful retry pattern: {retry_seconds}s delay worked "
                    f"for same customer+instrument+failure. Memory contribution: TIMING_MATCH."
                ),
                memory_contribution=MemoryContribution.FULL_CONTEXT,
                evidence_references=evidence_refs,
                discarded_evidence=bundle.discarded_evidence,
            )

        # Strategy 2: Permanent or instrument failure → suggest method
        if failure.failure_class in (FailureClass.PERMANENT, FailureClass.INSTRUMENT):
            alternative = self._find_alternative_method(bundle)
            return RecoveryDecision(
                failure_id=failure.id,
                action=RecoveryAction.SUGGEST_METHOD,
                recommended_method=alternative or "upi",
                confidence=0.75,
                reason=(
                    f"Permanent failure class ({failure.failure_class}). "
                    f"Blind retry of same method not useful. Suggesting alternative."
                ),
                memory_contribution=bundle.memory_contribution,
                evidence_references=bundle.accepted_evidence,
                discarded_evidence=bundle.discarded_evidence,
            )

        # Strategy 3: Have accepted evidence with successful alternative method
        alt_method_evidence = self._find_successful_alternative(bundle)
        if alt_method_evidence:
            method, confidence, refs = alt_method_evidence
            return RecoveryDecision(
                failure_id=failure.id,
                action=RecoveryAction.SUGGEST_METHOD,
                recommended_method=method,
                confidence=confidence,
                reason=(
                    f"Historical evidence shows {method} succeeded for this customer/merchant. "
                    f"Current method {failure.method} failed."
                ),
                memory_contribution=bundle.memory_contribution,
                evidence_references=refs,
                discarded_evidence=bundle.discarded_evidence,
            )

        # Strategy 4: Too many failed attempts → stop
        if bundle.retry_count >= 2:
            return RecoveryDecision(
                failure_id=failure.id,
                action=RecoveryAction.STOP,
                confidence=0.90,
                reason=f"Multiple failed recovery attempts ({bundle.retry_count}). Stopping to prevent further friction.",
                memory_contribution=bundle.memory_contribution,
                evidence_references=bundle.accepted_evidence,
                discarded_evidence=bundle.discarded_evidence,
            )

        # Strategy 5: Transient failure with no timing evidence → safe RETRY_AFTER with default
        if failure.failure_class == FailureClass.TRANSIENT:
            return RecoveryDecision(
                failure_id=failure.id,
                action=RecoveryAction.RETRY_AFTER,
                retry_after_seconds=DEFAULT_RETRY_SECONDS,
                recommended_method=failure.method,
                confidence=0.55,
                reason=(
                    f"Transient failure ({failure.failure_code}). "
                    f"No historical timing pattern — using safe default {DEFAULT_RETRY_SECONDS}s."
                ),
                memory_contribution=MemoryContribution.NONE,
                evidence_references=[],
                discarded_evidence=bundle.discarded_evidence,
            )

        # Strategy 6: No useful memory → safe fallback
        return self._safe_fallback(bundle)

    def _find_timing_evidence(
        self, bundle: EvidenceBundle
    ) -> tuple[int, list] | None:
        """
        Find accepted evidence with a successful retry timing pattern.
        Returns (retry_seconds, evidence_refs) or None.
        """
        if not bundle.accepted_evidence:
            return None

        failure = bundle.current_failure
        if failure.failure_class not in (FailureClass.TRANSIENT, FailureClass.UNKNOWN):
            return None

        timing_refs = []
        retry_intervals = []

        for ref in bundle.accepted_evidence:
            meta = ref.metadata or {}
            # Look for outcome nodes with retry timing
            if ref.memory_type in ("recovery_outcome", "recovery_decision"):
                retry_secs = meta.get("retry_after_seconds")
                outcome = meta.get("outcome", "")
                if retry_secs and outcome == "SUCCESS":
                    retry_intervals.append(int(retry_secs))
                    timing_refs.append(ref)

        if not retry_intervals:
            return None

        # Use median of successful intervals
        retry_intervals.sort()
        median_idx = len(retry_intervals) // 2
        suggested_seconds = retry_intervals[median_idx]

        LOGGER.debug(
            "Timing evidence: %d successful intervals, suggesting %ds",
            len(retry_intervals),
            suggested_seconds,
        )
        return suggested_seconds, timing_refs

    def _find_successful_alternative(
        self, bundle: EvidenceBundle
    ) -> tuple[str, float, list] | None:
        """Find evidence of a successful alternative payment method."""
        failure = bundle.current_failure
        method_success: dict[str, int] = {}
        method_refs: dict[str, list] = {}

        for ref in bundle.accepted_evidence:
            meta = ref.metadata or {}
            outcome = meta.get("outcome", "")
            action = meta.get("action_type", "")
            method = meta.get("recommended_method", "") or ""

            if outcome == "SUCCESS" and method and method != failure.method:
                method_success[method] = method_success.get(method, 0) + 1
                method_refs.setdefault(method, []).append(ref)

        if not method_success:
            return None

        # Pick most frequently successful alternative
        best_method = max(method_success, key=lambda m: method_success[m])
        count = method_success[best_method]
        confidence = min(0.9, 0.6 + 0.1 * count)

        return best_method, confidence, method_refs[best_method]

    def _find_alternative_method(self, bundle: EvidenceBundle) -> str | None:
        """Find any valid alternative payment method from instruments or evidence."""
        failure = bundle.current_failure
        # Check current instruments for alternatives
        for inst in bundle.current_instruments:
            if inst.fingerprint_or_safe_alias != failure.instrument_id:
                return inst.instrument_type
        # Common fallback alternatives
        alt_map = {"card": "upi", "upi": "netbanking", "netbanking": "wallet", "wallet": "card"}
        return alt_map.get(failure.method, "upi")

    def _safe_fallback(self, bundle: EvidenceBundle) -> RecoveryDecision:
        """Safe fallback when no useful memory exists."""
        failure = bundle.current_failure
        # Don't pretend memory helped when there is none
        return RecoveryDecision(
            failure_id=failure.id,
            action=RecoveryAction.CUSTOMER_NUDGE,
            confidence=0.40,
            reason=(
                "No reliable historical evidence available. "
                "Nudging customer to retry with fresh payment method. "
                "memory_contribution=NONE."
            ),
            memory_contribution=MemoryContribution.NONE,
            evidence_references=[],
            discarded_evidence=bundle.discarded_evidence,
        )


class LLMDecisionProvider(DecisionProvider):
    """
    Optional LLM-backed decision provider.
    The LLM selects ONLY from the bounded action set.
    All output is validated by PolicyEngine before execution.
    """

    def __init__(self, provider: str = "openai", model: str = "gpt-4o-mini") -> None:
        self.provider = provider
        self.model = model
        self._client = None

    def decide(self, bundle: EvidenceBundle) -> RecoveryDecision:
        """Ask LLM for a candidate decision, then validate it."""
        import time
        start = time.time()

        prompt = self._build_prompt(bundle)
        try:
            raw = self._call_llm(prompt)
            candidate = self._parse_response(raw, bundle)
            latency_ms = (time.time() - start) * 1000
            LOGGER.info("LLM decision: %s in %.0fms", candidate.action, latency_ms)
            return candidate
        except Exception as e:
            LOGGER.warning("LLM decision failed (%s), using fallback: %s", type(e).__name__, e)
            fallback = DeterministicDecisionProvider()
            return fallback.decide(bundle)

    def _build_prompt(self, bundle: EvidenceBundle) -> str:
        failure = bundle.current_failure
        accepted = bundle.accepted_evidence
        discarded = bundle.discarded_evidence

        evidence_text = ""
        for ref in accepted[:5]:
            evidence_text += f"\n- [{ref.waggle_node_id[:8]}] {ref.label} (score={ref.relevance_score:.2f}): {ref.metadata}"

        discarded_text = ""
        for ref in discarded[:3]:
            discarded_text += f"\n- [{ref.waggle_node_id[:8]}] REJECTED({ref.rejection_reason}): {ref.label}"

        policy = bundle.merchant_policy
        policy_text = "None" if not policy else (
            f"max_attempts={policy.max_recovery_attempts}, "
            f"min_interval={policy.min_retry_interval_seconds}s, "
            f"allowed={[a.value for a in policy.allowed_actions]}"
        )

        return f"""You are a payment recovery agent. Analyze this payment failure and choose ONE recovery action.

PAYMENT FAILURE:
- ID: {failure.external_payment_id}
- Customer: {failure.customer_id}
- Amount: ₹{failure.amount_rupees:.2f}
- Method: {failure.method} ({failure.instrument_id})
- Failure: {failure.failure_code} — {failure.failure_reason}
- Class: {failure.failure_class}
- Retry count: {bundle.retry_count}

ACCEPTED EVIDENCE (use these only):
{evidence_text or "None"}

DISCARDED EVIDENCE (do NOT use these):
{discarded_text or "None"}

MERCHANT POLICY:
{policy_text}

ALLOWED ACTIONS: RETRY_NOW, RETRY_AFTER, SUGGEST_METHOD, CUSTOMER_NUDGE, STOP

RULES:
- You MUST select exactly one action from the allowed list.
- Do NOT invent payment methods. Only suggest methods present in evidence or instruments.
- Do NOT cite discarded evidence.
- If evidence is insufficient, choose CUSTOMER_NUDGE or STOP.
- Return JSON only.

Return this JSON:
{{"action": "ACTION_NAME", "retry_after_seconds": null_or_integer, "recommended_method": null_or_string, "confidence": 0.0_to_1.0, "reason": "brief explanation citing evidence IDs"}}"""

    def _call_llm(self, prompt: str) -> str:
        if self.provider == "openai":
            import openai
            client = openai.OpenAI()
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or ""
        elif self.provider == "google":
            from google import genai
            client = genai.Client()
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
            )
            return response.text or ""
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def _parse_response(self, raw: str, bundle: EvidenceBundle) -> RecoveryDecision:
        """Parse LLM response into a bounded RecoveryDecision."""
        import json
        failure = bundle.current_failure

        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError(f"LLM returned non-JSON: {raw[:200]}")

        raw_action = str(data.get("action", "STOP")).upper().strip()
        # Validate action is in bounded set
        valid_actions = {a.value for a in RecoveryAction}
        if raw_action not in valid_actions:
            LOGGER.warning("LLM returned invalid action %s, defaulting to STOP", raw_action)
            raw_action = "STOP"

        action = RecoveryAction(raw_action)

        # Validate retry_after_seconds
        retry_seconds = data.get("retry_after_seconds")
        if retry_seconds is not None:
            try:
                retry_seconds = int(retry_seconds)
                retry_seconds = max(60, min(7200, retry_seconds))  # Clamp to sane range
            except (ValueError, TypeError):
                retry_seconds = None

        # Validate recommended_method — must be a known payment method, not invented
        recommended_method = data.get("recommended_method")
        if recommended_method:
            known_methods = {"card", "upi", "netbanking", "wallet", "emi", "paylater"}
            if str(recommended_method).lower() not in known_methods:
                LOGGER.warning("LLM invented method %s, clearing", recommended_method)
                recommended_method = None

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        return RecoveryDecision(
            failure_id=failure.id,
            action=action,
            retry_after_seconds=retry_seconds,
            recommended_method=recommended_method,
            confidence=confidence,
            reason=str(data.get("reason", "LLM decision"))[:500],
            memory_contribution=bundle.memory_contribution,
            evidence_references=bundle.accepted_evidence,
            discarded_evidence=bundle.discarded_evidence,
        )


def create_decision_provider(provider: str = "deterministic", **kwargs) -> DecisionProvider:
    """Factory for decision providers."""
    if provider == "llm":
        return LLMDecisionProvider(**kwargs)
    return DeterministicDecisionProvider()
