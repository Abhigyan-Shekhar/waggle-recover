"""Evidence scoring with configurable weights.

Every evidence item gets explicit score components for full transparency.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from app.domain.models import PaymentFailure

DEFAULT_WEIGHTS = {
    "semantic_relevance": 0.20,
    "customer_match": 0.20,
    "merchant_match": 0.10,
    "instrument_match": 0.20,
    "failure_match": 0.15,
    "recency": 0.05,
    "successful_outcome": 0.10,
}

RECENCY_HALF_LIFE_DAYS = 14.0


def score_evidence(
    node: dict[str, Any],
    failure: PaymentFailure,
    current_instrument_alias: str,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    Score a candidate evidence node against the current failure.

    Returns:
        (total_score, component_scores)
    """
    w = weights or DEFAULT_WEIGHTS
    tags = node.get("tags", [])
    components: dict[str, float] = {}

    # 1. Semantic relevance (from Waggle scoring if available)
    final_score = node.get("final_score") or node.get("similarity_score") or 0.0
    components["semantic_relevance"] = float(final_score)

    # 2. Customer match
    customer_match = 1.0 if f"customer:{failure.customer_id}" in tags else 0.0
    components["customer_match"] = customer_match

    # 3. Merchant match
    merchant_match = 1.0 if f"merchant:{failure.merchant_id}" in tags else 0.0
    components["merchant_match"] = merchant_match

    # 4. Instrument match
    instrument_match = 0.0
    if current_instrument_alias:
        if f"instrument:{current_instrument_alias}" in tags:
            instrument_match = 1.0
        elif any(t.startswith("instrument:") for t in tags):
            instrument_match = 0.3  # Partial — same instrument type but different alias
    components["instrument_match"] = instrument_match

    # 5. Failure reason match
    failure_match = 0.0
    if failure.failure_code:
        failure_tag = f"failure_reason:{failure.failure_code}"
        if failure_tag in tags:
            failure_match = 1.0
        else:
            # Partial match on failure class
            fc_tag = f"failure_class:{failure.failure_class}"
            if fc_tag in tags:
                failure_match = 0.5
    components["failure_match"] = failure_match

    # 6. Recency score
    recency = _compute_recency(node)
    components["recency"] = recency

    # 7. Successful outcome signal
    success_signal = 0.0
    if "outcome:success" in tags:
        success_signal = 1.0
    elif "outcome:failure" in tags:
        success_signal = -0.2  # Penalize failed patterns slightly
    components["successful_outcome"] = max(0.0, success_signal)

    # Weighted sum
    total = sum(w.get(k, 0.0) * v for k, v in components.items())
    total = max(0.0, min(1.0, total))

    return total, components


def _compute_recency(node: dict[str, Any]) -> float:
    """Exponential decay from event time, never ingestion time when available."""
    metadata = node.get("metadata", {}) or {}
    created_at_str = (metadata.get("executed_at") or metadata.get("occurred_at")
                      or node.get("valid_from") or node.get("created_at"))
    if not created_at_str:
        return 0.5  # Unknown recency — neutral

    try:
        created_at = datetime.fromisoformat(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        age_days = (now - created_at).total_seconds() / 86400.0
        return math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)
    except Exception:
        return 0.5
