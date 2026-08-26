"""Evaluation metrics."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SystemMetrics:
    """Aggregate metrics for one system across all scenarios."""
    name: str
    scenario_count: int = 0

    # Core recovery metrics
    correct_action_count: int = 0
    success_count: int = 0
    total_recovered_amount: int = 0
    total_amount_at_risk: int = 0

    # Memory quality metrics (Waggle Recover only)
    stale_evidence_detected: int = 0
    stale_evidence_correctly_rejected: int = 0
    memory_contribution_count: int = 0
    evidence_accepted_total: int = 0
    evidence_discarded_total: int = 0

    # Latency
    total_latency_ms: float = 0.0
    decision_latencies: list[float] = field(default_factory=list)

    # Category breakdown
    category_results: dict[str, dict] = field(default_factory=dict)

    @property
    def action_accuracy(self) -> float:
        if self.scenario_count == 0:
            return 0.0
        return self.correct_action_count / self.scenario_count

    @property
    def success_rate(self) -> float:
        if self.scenario_count == 0:
            return 0.0
        return self.success_count / self.scenario_count

    @property
    def recovery_rate_gmv(self) -> float:
        if self.total_amount_at_risk == 0:
            return 0.0
        return self.total_recovered_amount / self.total_amount_at_risk

    @property
    def stale_rejection_rate(self) -> float:
        if self.stale_evidence_detected == 0:
            return 0.0
        return self.stale_evidence_correctly_rejected / self.stale_evidence_detected

    @property
    def avg_latency_ms(self) -> float:
        if not self.decision_latencies:
            return 0.0
        return sum(self.decision_latencies) / len(self.decision_latencies)

    @property
    def p95_latency_ms(self) -> float:
        if not self.decision_latencies:
            return 0.0
        sorted_l = sorted(self.decision_latencies)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scenario_count": self.scenario_count,
            "action_accuracy": round(self.action_accuracy, 4),
            "action_accuracy_pct": round(self.action_accuracy * 100, 1),
            "success_rate": round(self.success_rate, 4),
            "success_rate_pct": round(self.success_rate * 100, 1),
            "recovery_rate_gmv": round(self.recovery_rate_gmv, 4),
            "recovery_rate_gmv_pct": round(self.recovery_rate_gmv * 100, 1),
            "total_recovered_amount": self.total_recovered_amount,
            "total_amount_at_risk": self.total_amount_at_risk,
            "stale_evidence_detected": self.stale_evidence_detected,
            "stale_evidence_correctly_rejected": self.stale_evidence_correctly_rejected,
            "stale_rejection_rate": round(self.stale_rejection_rate, 4),
            "stale_rejection_rate_pct": round(self.stale_rejection_rate * 100, 1),
            "memory_contribution_count": self.memory_contribution_count,
            "evidence_accepted_total": self.evidence_accepted_total,
            "evidence_discarded_total": self.evidence_discarded_total,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "category_results": self.category_results,
        }


@dataclass
class ComparisonSummary:
    """Side-by-side comparison of all three systems."""
    baseline_a: SystemMetrics
    baseline_b: SystemMetrics
    system_c: SystemMetrics
    scenario_count: int

    def to_dict(self) -> dict[str, Any]:
        a = self.baseline_a.to_dict()
        b = self.baseline_b.to_dict()
        c = self.system_c.to_dict()

        return {
            "scenario_count": self.scenario_count,
            "systems": {"baseline_a": a, "baseline_b": b, "system_c": c},
            "improvements": {
                "c_vs_a_accuracy": round(c["action_accuracy"] - a["action_accuracy"], 4),
                "c_vs_b_accuracy": round(c["action_accuracy"] - b["action_accuracy"], 4),
                "c_vs_a_gmv_recovery": round(c["recovery_rate_gmv"] - a["recovery_rate_gmv"], 4),
                "c_vs_b_gmv_recovery": round(c["recovery_rate_gmv"] - b["recovery_rate_gmv"], 4),
                "stale_rejection_improvement_vs_b": round(
                    c["stale_rejection_rate"] - b.get("stale_rejection_rate", 0.0), 4
                ),
            },
            "legend": {
                "baseline_a": "Blind Fixed Retry — always retries same method after fixed delay",
                "baseline_b": "Contextual History — uses history, no supersession validation",
                "system_c": "Waggle Recover — temporal memory graph with supersession validation",
            },
        }

    def format_table(self) -> str:
        """Human-readable comparison table."""
        a = self.baseline_a
        b = self.baseline_b
        c = self.system_c

        lines = [
            "",
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║             WAGGLE RECOVER — EVALUATION RESULTS                      ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            f"║  Scenarios: {self.scenario_count:<60}║",
            "╠══════════════════╦═══════════════╦═══════════════╦═══════════════╣",
            "║ Metric           ║ Baseline A    ║ Baseline B    ║ System C      ║",
            "╠══════════════════╬═══════════════╬═══════════════╬═══════════════╣",
            f"║ Action Accuracy  ║ {f'{a.action_accuracy:.1%}'.ljust(13)} ║ {f'{b.action_accuracy:.1%}'.ljust(13)} ║ {f'{c.action_accuracy:.1%}'.ljust(13)} ║",
            f"║ Success Rate     ║ {f'{a.success_rate:.1%}'.ljust(13)} ║ {f'{b.success_rate:.1%}'.ljust(13)} ║ {f'{c.success_rate:.1%}'.ljust(13)} ║",
            f"║ GMV Recovery %   ║ {f'{a.recovery_rate_gmv:.1%}'.ljust(13)} ║ {f'{b.recovery_rate_gmv:.1%}'.ljust(13)} ║ {f'{c.recovery_rate_gmv:.1%}'.ljust(13)} ║",
            f"║ Stale Rejection  ║ {'N/A'.ljust(13)} ║ {'~0%'.ljust(13)} ║ {f'{c.stale_rejection_rate:.1%}'.ljust(13)} ║",
            f"║ Avg Latency ms   ║ {a.avg_latency_ms:<13.1f} ║ {b.avg_latency_ms:<13.1f} ║ {c.avg_latency_ms:<13.1f} ║",
            "╠══════════════════╩═══════════════╩═══════════════╩═══════════════╣",
            f"║ C vs A accuracy: +{f'{c.action_accuracy - a.action_accuracy:.1%}'.ljust(64)}║",
            f"║ C vs B accuracy: +{f'{c.action_accuracy - b.action_accuracy:.1%}'.ljust(64)}║",
            "╚══════════════════════════════════════════════════════════════════════╝",
            "",
        ]
        return "\n".join(lines)
