"""Human-readable explanations and audit trail assembly."""
from __future__ import annotations

from app.domain.enums import PolicyResult, TemporalStatus
from app.domain.models import EvidenceBundle, RecoveryDecision
from app.recovery.policy import PolicyValidationResult


def build_explanation(
    bundle: EvidenceBundle,
    decision: RecoveryDecision,
    policy_result: PolicyValidationResult,
) -> str:
    """
    Build a human-readable explanation for a recovery decision.
    Cites actual evidence node IDs.
    """
    lines: list[str] = []
    failure = bundle.current_failure

    # Header
    lines.append("═══════════════════════════════════════════")
    lines.append("RECOVERY DECISION EXPLANATION")
    lines.append("═══════════════════════════════════════════")
    lines.append("")

    # Current failure
    lines.append("CURRENT FAILURE")
    lines.append(f"  Payment: {failure.external_payment_id}")
    lines.append(f"  Customer: {failure.customer_id}")
    lines.append(f"  Merchant: {failure.merchant_id}")
    lines.append(f"  Amount: ₹{failure.amount_rupees:.2f}")
    lines.append(f"  Method: {failure.method} ({failure.instrument_id or 'N/A'})")
    lines.append(f"  Failure: {failure.failure_code} — {failure.failure_reason}")
    lines.append(f"  Class: {failure.failure_class}")
    lines.append("")

    # Memory retrieval
    lines.append("MEMORY RETRIEVAL")
    lines.append(f"  Mode: {bundle.retrieval_mode}")
    lines.append(f"  Contribution: {bundle.memory_contribution}")
    lines.append(f"  Accepted evidence: {len(bundle.accepted_evidence)} nodes")
    lines.append(f"  Discarded evidence: {len(bundle.discarded_evidence)} nodes")
    lines.append(f"  Latency: {bundle.retrieval_latency_ms:.1f}ms")
    lines.append("")

    # Accepted evidence
    if bundle.accepted_evidence:
        lines.append("ACCEPTED EVIDENCE")
        for ref in bundle.accepted_evidence:
            lines.append(f"  ✓ [{ref.waggle_node_id[:8]}] {ref.label}")
            lines.append(f"      type={ref.memory_type}, score={ref.relevance_score:.3f}")
            if ref.score_components:
                comp_str = ", ".join(f"{k}={v:.2f}" for k, v in ref.score_components.items() if v > 0)
                lines.append(f"      components: {comp_str}")
        lines.append("")

    # Discarded evidence — THIS IS THE CRITICAL SECTION
    if bundle.discarded_evidence:
        lines.append("DISCARDED EVIDENCE (NOT used in decision)")
        for ref in bundle.discarded_evidence:
            status_icon = "⚠" if ref.temporal_status == TemporalStatus.SUPERSEDED else "✗"
            lines.append(f"  {status_icon} [{ref.waggle_node_id[:8]}] {ref.label}")
            lines.append(f"      Status: {ref.temporal_status}")
            lines.append(f"      Reason: {ref.rejection_reason}")
        lines.append("")

    if bundle.strategy_priors:
        lines.append("ADAPTIVE STRATEGY MEMORY (authoritative outcomes only)")
        for prior in bundle.strategy_priors:
            method = f" → {prior.recommended_method}" if prior.recommended_method else ""
            readiness = "insufficient history" if prior.insufficient_history else "eligible for safe ranking"
            lines.append(
                f"  {prior.action}{method}: posterior={prior.posterior_success_probability:.1%}, "
                f"effective_n={prior.effective_n:.1f} ({readiness})"
            )
        lines.append("  These priors rank viable actions only; PolicyEngine remains final authority.")
        lines.append("")

    # Policy checks
    lines.append("POLICY CHECKS")
    for check in policy_result.checks:
        icon = "✓" if check.passed else "✗"
        lines.append(f"  {icon} {check.check_name}: {check.note}")
    if policy_result.result == PolicyResult.BLOCK:
        lines.append(f"  ⛔ BLOCKED: {policy_result.block_reason}")
    elif policy_result.result == PolicyResult.MODIFY:
        lines.append("  ⚠ MODIFIED: action or timing adjusted to comply with policy")
    lines.append("")

    # Final action
    lines.append("FINAL DECISION")
    lines.append(f"  Action: {decision.action}")
    if decision.retry_after_seconds is not None:
        lines.append(f"  Retry after: {decision.retry_after_seconds}s ({decision.retry_after_seconds // 60}min)")
    if decision.recommended_method:
        lines.append(f"  Recommended method: {decision.recommended_method}")
    lines.append(f"  Confidence: {decision.confidence:.0%}")
    lines.append(f"  Reason: {decision.reason}")
    lines.append("")

    lines.append("═══════════════════════════════════════════")
    return "\n".join(lines)


def build_structured_audit(
    bundle: EvidenceBundle,
    decision: RecoveryDecision,
    policy_result: PolicyValidationResult,
) -> dict:
    """Build structured audit record for API/storage."""
    return {
        "decision_id": decision.id,
        "failure_id": decision.failure_id,
        "failure": {
            "external_payment_id": bundle.current_failure.external_payment_id,
            "customer_id": bundle.current_failure.customer_id,
            "merchant_id": bundle.current_failure.merchant_id,
            "amount": bundle.current_failure.amount,
            "method": bundle.current_failure.method,
            "instrument_id": bundle.current_failure.instrument_id,
            "failure_code": bundle.current_failure.failure_code,
            "failure_class": bundle.current_failure.failure_class,
        },
        "retrieval": {
            "mode": bundle.retrieval_mode,
            "memory_contribution": bundle.memory_contribution,
            "latency_ms": bundle.retrieval_latency_ms,
            "accepted_count": len(bundle.accepted_evidence),
            "discarded_count": len(bundle.discarded_evidence),
        },
        "accepted_evidence": [
            {
                "node_id": r.waggle_node_id,
                "label": r.label,
                "memory_type": r.memory_type,
                "relevance_score": r.relevance_score,
                "temporal_status": r.temporal_status,
                "score_components": r.score_components,
            }
            for r in bundle.accepted_evidence
        ],
        "discarded_evidence": [
            {
                "node_id": r.waggle_node_id,
                "label": r.label,
                "memory_type": r.memory_type,
                "temporal_status": r.temporal_status,
                "rejection_reason": r.rejection_reason,
            }
            for r in bundle.discarded_evidence
        ],
        "policy": {
            "result": policy_result.result,
            "checks": [
                {"check": c.check_name, "passed": c.passed, "note": c.note}
                for c in policy_result.checks
            ],
            "block_reason": policy_result.block_reason,
        },
        "decision": {
            "action": decision.action,
            "retry_after_seconds": decision.retry_after_seconds,
            "recommended_method": decision.recommended_method,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "memory_contribution": decision.memory_contribution,
        },
    }
