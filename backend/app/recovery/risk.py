"""Explainable revenue-risk prioritization; never an action-policy override."""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import FailureClass, TemporalStatus
from app.domain.models import EvidenceBundle


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    band: str
    factors: list[str]

    def to_dict(self) -> dict[str, object]:
        return {"risk_score": self.score, "risk_band": self.band, "risk_factors": self.factors}


def assess_revenue_risk(bundle: EvidenceBundle) -> RiskAssessment:
    """Score observable risk signals for queue ordering, not decision authority."""
    failure = bundle.current_failure
    score = 20
    factors: list[str] = []

    if failure.amount >= 1_000_000:
        score += 25
        factors.append("+ high payment value")
    elif failure.amount >= 500_000:
        score += 15
        factors.append("+ elevated payment value")

    if bundle.retry_count:
        retry_points = min(30, bundle.retry_count * 15)
        score += retry_points
        factors.append(f"+ {bundle.retry_count + 1}th recovery attempt")

    class_points = {
        FailureClass.PERMANENT: 18,
        FailureClass.INSTRUMENT: 18,
        FailureClass.BALANCE: 12,
        FailureClass.ROUTE: 8,
        FailureClass.TRANSIENT: 5,
        FailureClass.UNKNOWN: 10,
    }[failure.failure_class]
    score += class_points
    factors.append(f"+ {failure.failure_class.value.lower()} failure class")

    active_instruments = [item for item in bundle.current_instruments if item.status == "active"]
    if active_instruments:
        score -= 5
        factors.append("- active payment instrument available")
    else:
        score += 18
        factors.append("+ no active payment instrument recorded")

    conflicts = [
        item for item in (*bundle.accepted_evidence, *bundle.discarded_evidence)
        if item.temporal_status == TemporalStatus.CONFLICTING
    ]
    if conflicts:
        score += 20
        factors.append("+ authoritative history is conflicting")

    authoritative_success = any(
        item.accepted and str(item.metadata.get("outcome", "")) == "SUCCESS"
        for item in bundle.accepted_evidence
    )
    if authoritative_success:
        score -= 10
        factors.append("- previous authoritative recovery exists")

    score = max(0, min(100, score))
    band = "CRITICAL" if score >= 85 else "HIGH" if score >= 65 else "MEDIUM" if score >= 35 else "LOW"
    return RiskAssessment(score=score, band=band, factors=factors)
