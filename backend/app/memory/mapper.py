"""Maps domain models to/from Waggle node/edge structures.

Uses only existing Waggle NodeType and RelationType enums.
Domain semantics are encoded via tags and metadata.
"""
from __future__ import annotations

from typing import Any

from waggle.models import NodeType

# ── Node content templates ──────────────────────────────────────────────────


def failure_content(failure_data: dict[str, Any]) -> str:
    amount_rupees = failure_data.get("amount", 0) / 100.0
    return (
        f"Payment {failure_data.get('external_payment_id', 'unknown')} "
        f"for customer {failure_data.get('customer_id', '?')} "
        f"at merchant {failure_data.get('merchant_id', '?')} "
        f"failed using {failure_data.get('instrument_id', failure_data.get('method', '?'))} "
        f"for ₹{amount_rupees:.2f} because {failure_data.get('failure_code', 'unknown')}. "
        f"Reason: {failure_data.get('failure_reason', 'N/A')}. "
        f"Class: {failure_data.get('failure_class', 'UNKNOWN')}."
    )


def instrument_content(instr_data: dict[str, Any]) -> str:
    alias = instr_data.get("fingerprint_or_safe_alias", "unknown")
    itype = instr_data.get("instrument_type", "card")
    status = instr_data.get("status", "active")
    supersedes = instr_data.get("supersedes_instrument_id")
    base = f"Payment instrument {alias} (type: {itype}, status: {status}) for customer {instr_data.get('customer_id', '?')}."
    if supersedes:
        base += f" Supersedes instrument {supersedes}."
    return base


def decision_content(dec_data: dict[str, Any]) -> str:
    content = (
        f"Recovery decision for failure {dec_data.get('failure_id', '?')}: "
        f"action={dec_data.get('action', '?')}, "
        f"confidence={dec_data.get('confidence', 0):.2f}. "
        f"Reason: {dec_data.get('reason', 'N/A')}. "
        f"Memory contribution: {dec_data.get('memory_contribution', 'NONE')}."
    )
    if dec_data.get("human_review_required"):
        content += (
            " Human review required: autonomous recovery was blocked by policy."
            f" Reason: {dec_data.get('escalation_reason', 'No safe automated recovery remains')}."
        )
    return content


def outcome_content(outcome_data: dict[str, Any]) -> str:
    outcome = outcome_data.get("outcome", "PENDING")
    action = outcome_data.get("action_type", "?")
    recovered = outcome_data.get("recovered_amount", 0) / 100.0
    return (
        f"Recovery outcome for failure {outcome_data.get('failure_id', '?')}: "
        f"action={action}, outcome={outcome}, "
        f"recovered=₹{recovered:.2f}. "
        f"Customer: {outcome_data.get('customer_id', '?')}, "
        f"merchant: {outcome_data.get('merchant_id', '?')}, "
        f"instrument: {outcome_data.get('instrument_id', '?')}, "
        f"failure_code: {outcome_data.get('failure_code', '?')}."
    )


def policy_content(policy_data: dict[str, Any]) -> str:
    return (
        f"Merchant policy {policy_data.get('policy_id', '?')} "
        f"version {policy_data.get('version', 1)} for {policy_data.get('merchant_id', '?')}: "
        f"max_attempts={policy_data.get('max_recovery_attempts', 3)}, "
        f"min_interval={policy_data.get('min_retry_interval_seconds', 300)}s, "
        f"max_interval={policy_data.get('max_retry_interval_seconds', 3600)}s. "
        f"Allowed actions: {policy_data.get('allowed_actions', [])}. "
        f"Blocked methods: {policy_data.get('blocked_methods', [])}."
    )


# ── NodeType + tags mapping ──────────────────────────────────────────────────


def failure_node_type() -> NodeType:
    return NodeType.FACT


def instrument_node_type() -> NodeType:
    return NodeType.ENTITY


def decision_node_type() -> NodeType:
    return NodeType.DECISION


def outcome_node_type() -> NodeType:
    return NodeType.FACT


def policy_node_type() -> NodeType:
    return NodeType.PREFERENCE


def failure_tags(failure_data: dict[str, Any]) -> list[str]:
    tags = [
        "payment_failure",
        f"customer:{failure_data.get('customer_id', '')}",
        f"merchant:{failure_data.get('merchant_id', '')}",
        f"method:{failure_data.get('method', '')}",
    ]
    if failure_data.get("failure_code"):
        tags.append(f"failure_reason:{failure_data['failure_code']}")
    if failure_data.get("instrument_id"):
        tags.append(f"instrument:{failure_data['instrument_id']}")
    if failure_data.get("failure_class"):
        tags.append(f"failure_class:{failure_data['failure_class']}")
    return tags


def instrument_tags(instr_data: dict[str, Any]) -> list[str]:
    tags = [
        "payment_instrument",
        f"customer:{instr_data.get('customer_id', '')}",
        f"instrument_type:{instr_data.get('instrument_type', '')}",
        f"instrument:{instr_data.get('fingerprint_or_safe_alias', '')}",
    ]
    if instr_data.get("status"):
        tags.append(f"status:{instr_data['status']}")
    return tags


def decision_tags(dec_data: dict[str, Any]) -> list[str]:
    tags = [
        "recovery_decision",
        f"action:{dec_data.get('action', '').lower()}",
        f"customer:{dec_data.get('customer_id', '')}",
        f"merchant:{dec_data.get('merchant_id', '')}",
    ]
    if dec_data.get("recommended_method"):
        tags.append(f"method:{dec_data['recommended_method']}")
    if dec_data.get("human_review_required"):
        tags.append("human_review_required")
        tags.append("policy_blocked")
    return tags


def outcome_tags(outcome_data: dict[str, Any]) -> list[str]:
    tags = [
        "recovery_outcome",
        f"outcome:{outcome_data.get('outcome', 'PENDING').lower()}",
        f"customer:{outcome_data.get('customer_id', '')}",
        f"merchant:{outcome_data.get('merchant_id', '')}",
        f"action:{outcome_data.get('action_type', '').lower()}",
    ]
    if outcome_data.get("recommended_method"):
        tags.append(f"method:{outcome_data['recommended_method']}")
    if outcome_data.get("instrument_id"):
        tags.append(f"instrument:{outcome_data['instrument_id']}")
    if outcome_data.get("failure_code"):
        tags.append(f"failure_reason:{outcome_data['failure_code']}")
    return tags


def policy_tags(policy_data: dict[str, Any]) -> list[str]:
    return [
        "merchant_policy",
        f"merchant:{policy_data.get('merchant_id', '')}",
        f"policy:{policy_data.get('policy_id', '')}",
        f"policy_version:{policy_data.get('version', 1)}",
    ]


# ── Metadata builders ──────────────────────────────────────────────────────


def failure_metadata(failure_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "failure_id": failure_data.get("id", ""),
        "external_payment_id": failure_data.get("external_payment_id", ""),
        "customer_id": failure_data.get("customer_id", ""),
        "merchant_id": failure_data.get("merchant_id", ""),
        "amount": failure_data.get("amount", 0),
        "currency": failure_data.get("currency", "INR"),
        "method": failure_data.get("method", ""),
        "instrument_id": failure_data.get("instrument_id", ""),
        "failure_code": failure_data.get("failure_code", ""),
        "failure_class": failure_data.get("failure_class", "UNKNOWN"),
        "occurred_at": failure_data.get("occurred_at", ""),
        "recovery_episode_id": failure_data.get("recovery_episode_id", ""),
    }


def instrument_metadata(instr_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "instrument_id": instr_data.get("id", ""),
        "customer_id": instr_data.get("customer_id", ""),
        "instrument_type": instr_data.get("instrument_type", ""),
        "alias": instr_data.get("fingerprint_or_safe_alias", ""),
        "status": instr_data.get("status", "active"),
        "supersedes": instr_data.get("supersedes_instrument_id"),
    }


def decision_metadata(dec_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": dec_data.get("id", ""),
        "failure_id": dec_data.get("failure_id", ""),
        "action": dec_data.get("action", ""),
        "confidence": dec_data.get("confidence", 0.0),
        "evidence_confidence": dec_data.get("evidence_confidence", 0.0),
        "evidence_quality": dec_data.get("evidence_quality", "UNKNOWN"),
        "uncertainty_reason": dec_data.get("uncertainty_reason", ""),
        "abstention_reason": dec_data.get("abstention_reason", ""),
        "risk_score": dec_data.get("risk_score", 0),
        "risk_band": dec_data.get("risk_band", "LOW"),
        "risk_factors": dec_data.get("risk_factors", []),
        "recovery_episode_id": dec_data.get("recovery_episode_id", ""),
        "memory_contribution": dec_data.get("memory_contribution", "NONE"),
        "retry_after_seconds": dec_data.get("retry_after_seconds"),
        "recommended_method": dec_data.get("recommended_method"),
        "policy_result": dec_data.get("policy_result", "ALLOW"),
        "human_review_required": dec_data.get("human_review_required", False),
        "escalation_reason": dec_data.get("escalation_reason", ""),
        "attempt_count": dec_data.get("attempt_count", 0),
        "max_automated_attempts": dec_data.get("max_automated_attempts", 0),
        "last_safe_action": dec_data.get("last_safe_action"),
    }


def outcome_metadata(outcome_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": outcome_data.get("id", ""),
        "failure_id": outcome_data.get("failure_id", ""),
        "action_type": outcome_data.get("action_type", ""),
        "outcome": outcome_data.get("outcome", "PENDING"),
        "recovered_amount": outcome_data.get("recovered_amount", 0),
        "customer_id": outcome_data.get("customer_id", ""),
        "merchant_id": outcome_data.get("merchant_id", ""),
        "recommended_method": outcome_data.get("recommended_method"),
        "retry_after_seconds": outcome_data.get("retry_after_seconds"),
        "method": outcome_data.get("method", ""),
        "instrument_id": outcome_data.get("instrument_id", ""),
        "failure_code": outcome_data.get("failure_code", ""),
        "executed_at": outcome_data.get("executed_at", ""),
        "recovery_episode_id": outcome_data.get("recovery_episode_id", ""),
    }


# ── Query string builders for retrieval ───────────────────────────────────


def customer_failure_query(customer_id: str, failure_code: str = "") -> str:
    parts = [f"customer {customer_id} payment failure"]
    if failure_code:
        parts.append(failure_code)
    return " ".join(parts)


def instrument_query(customer_id: str, alias: str) -> str:
    return f"payment instrument {alias} customer {customer_id}"


def recovery_outcome_query(customer_id: str, merchant_id: str) -> str:
    return f"recovery outcome customer {customer_id} merchant {merchant_id}"


def merchant_policy_query(merchant_id: str) -> str:
    return f"merchant policy {merchant_id}"
