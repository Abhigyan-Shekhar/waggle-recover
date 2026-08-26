"""Synthetic scenario generator for evaluation harness.

Generates 200+ deterministic scenarios across categories:
- transient issuer failures
- expired instruments
- insufficient balance
- temporary route degradation
- method replacement
- merchant-specific history
- no-history controls
- etc.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import RecoveryAction


@dataclass
class ScenarioHistory:
    """A single historical event in a scenario."""
    event_type: str  # failure / success / instrument_added
    payment_id: str
    customer_id: str
    merchant_id: str
    amount: int
    method: str
    instrument_id: str
    failure_code: str = ""
    outcome: str = ""  # SUCCESS / FAILURE
    action_taken: str = ""
    retry_after_seconds: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class EvalScenario:
    """A single evaluation scenario."""
    id: str
    name: str
    category: str
    customer_id: str
    merchant_id: str
    amount: int
    method: str
    instrument_id: str
    failure_code: str
    failure_reason: str
    history: list[ScenarioHistory]

    # Ground truth — what actions will actually succeed
    action_outcomes: dict[str, str]  # RecoveryAction → "SUCCESS" / "FAILURE" / "SKIPPED"
    ground_truth_actions: list[str]  # Correct actions the agent should prefer

    # Expected memory contribution
    has_useful_memory: bool = True
    has_stale_memory: bool = False
    stale_instrument: str | None = None
    current_instrument: str | None = None

    # Instrument chain
    instruments: list[dict[str, Any]] = field(default_factory=list)

    seed: int = 42


# ── Category constants ─────────────────────────────────────────────────────

CUSTOMERS = [f"CUST-{i:04d}" for i in range(1, 51)]
MERCHANTS = [f"MERCH-{i:03d}" for i in range(1, 11)]
AMOUNTS = [500_00, 1000_00, 2500_00, 5000_00, 8000_00, 12000_00, 25000_00]  # paise

TRANSIENT_CODES = [
    ("issuer_unavailable", "Issuer temporarily unavailable"),
    ("network_error", "Network connectivity issue"),
    ("gateway_timeout", "Payment gateway timeout"),
]

PERMANENT_CODES = [
    ("expired_card", "Card has expired"),
    ("card_blocked", "Card has been blocked"),
    ("do_not_honour", "Issuer declined payment - do not honour"),
]

BALANCE_CODES = [
    ("insufficient_funds", "Insufficient balance in account"),
    ("credit_limit_exceeded", "Credit limit exceeded"),
]

METHODS = ["card", "upi", "netbanking", "wallet"]
INSTRUMENTS = {
    "card": ["card_1234", "card_5678", "card_9988", "card_4321"],
    "upi": ["upi_primary", "upi_secondary", "upi_work"],
    "netbanking": ["nb_hdfc", "nb_sbi", "nb_icici"],
    "wallet": ["wallet_paytm", "wallet_phonepe"],
}


def _ts(days_ago: float, hours_ago: float = 0) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago)


class ScenarioGenerator:
    """Generates stratified evaluation scenarios."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.rng = random.Random(seed)

    def generate(self, count: int = 200) -> list[EvalScenario]:
        """Generate `count` scenarios across all categories."""
        scenarios: list[EvalScenario] = []

        # Curated scenarios (always included)
        scenarios.extend(self._curated_scenarios())

        # Stratified synthetic scenarios
        categories = [
            ("transient_issuer", 35),
            ("expired_instrument", 20),
            ("insufficient_balance", 15),
            ("route_degradation", 15),
            ("method_replacement", 25),
            ("merchant_specific", 20),
            ("customer_specific", 20),
            ("conflicting_history", 10),
            ("no_history_control", 20),
            ("retry_limit_exhaustion", 15),
            ("successful_alternative", 20),
            ("failed_alternative", 15),
        ]

        remaining = max(0, count - len(scenarios))
        total_weight = sum(weight for _, weight in categories)
        # Largest-remainder apportionment preserves the intended category mix
        # while guaranteeing the evaluator receives exactly the requested count.
        raw = [(category, weight * remaining / total_weight) for category, weight in categories]
        allocated = {category: int(value) for category, value in raw}
        for category, _ in sorted(raw, key=lambda item: item[1] - int(item[1]), reverse=True)[:remaining - sum(allocated.values())]:
            allocated[category] += 1

        for category, _ in categories:
            n = allocated[category]
            for i in range(n):
                s = self._generate_scenario(category, i)
                if s:
                    scenarios.append(s)

        # Assign sequential IDs
        for idx, s in enumerate(scenarios):
            s.id = f"eval_{idx:04d}"

        return scenarios[:count]

    def _curated_scenarios(self) -> list[EvalScenario]:
        """The 5 curated + 1 mandate scenarios."""
        scenarios = []

        # Scenario 1 — Naive retry wastes attempts (bad retry prevention)
        scenarios.append(EvalScenario(
            id="curated_001",
            name="Bad Retry Prevention",
            category="successful_alternative",
            customer_id="CUST-1001",
            merchant_id="MERCH-001",
            amount=5000_00,
            method="card",
            instrument_id="card_1234",
            failure_code="issuer_unavailable",
            failure_reason="Issuer temporarily unavailable",
            history=[
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_001a",
                    customer_id="CUST-1001", merchant_id="MERCH-001",
                    amount=3000_00, method="card", instrument_id="card_1234",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    timestamp=_ts(10),
                ),
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_001b",
                    customer_id="CUST-1001", merchant_id="MERCH-001",
                    amount=3000_00, method="card", instrument_id="card_1234",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    action_taken="RETRY_NOW", timestamp=_ts(10, hours_ago=0.05),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_001c",
                    customer_id="CUST-1001", merchant_id="MERCH-001",
                    amount=3000_00, method="upi", instrument_id="upi_primary",
                    failure_code="", outcome="SUCCESS",
                    action_taken="SUGGEST_METHOD", timestamp=_ts(10, hours_ago=-0.1),
                ),
            ],
            action_outcomes={
                "RETRY_NOW": "FAILURE",
                "RETRY_AFTER": "FAILURE",
                "SUGGEST_METHOD": "SUCCESS",
                "CUSTOMER_NUDGE": "SUCCESS",
                "STOP": "SKIPPED",
            },
            ground_truth_actions=["SUGGEST_METHOD"],
            has_useful_memory=True,
            instruments=[
                {"alias": "card_1234", "type": "card", "status": "active"},
                {"alias": "upi_primary", "type": "upi", "status": "active"},
            ],
        ))

        # Scenario 2 — Timing memory
        scenarios.append(EvalScenario(
            id="curated_002",
            name="Timing Memory",
            category="transient_issuer",
            customer_id="CUST-1002",
            merchant_id="MERCH-001",
            amount=6000_00,
            method="card",
            instrument_id="card_5678",
            failure_code="issuer_unavailable",
            failure_reason="Issuer temporarily unavailable",
            history=[
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_002a",
                    customer_id="CUST-1002", merchant_id="MERCH-001",
                    amount=5000_00, method="card", instrument_id="card_5678",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    timestamp=_ts(16),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_002b",
                    customer_id="CUST-1002", merchant_id="MERCH-001",
                    amount=5000_00, method="card", instrument_id="card_5678",
                    outcome="SUCCESS", action_taken="RETRY_AFTER",
                    retry_after_seconds=480, timestamp=_ts(16, hours_ago=-0.14),
                ),
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_002c",
                    customer_id="CUST-1002", merchant_id="MERCH-001",
                    amount=6000_00, method="card", instrument_id="card_5678",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    timestamp=_ts(9),
                ),
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_002d",
                    customer_id="CUST-1002", merchant_id="MERCH-001",
                    amount=6000_00, method="card", instrument_id="card_5678",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    action_taken="RETRY_NOW", timestamp=_ts(9, hours_ago=0.05),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_002e",
                    customer_id="CUST-1002", merchant_id="MERCH-001",
                    amount=6000_00, method="card", instrument_id="card_5678",
                    outcome="SUCCESS", action_taken="RETRY_AFTER",
                    retry_after_seconds=600, timestamp=_ts(9, hours_ago=-0.18),
                ),
            ],
            action_outcomes={
                "RETRY_NOW": "FAILURE",
                "RETRY_AFTER": "SUCCESS",  # ~8-10 min window
                "SUGGEST_METHOD": "SUCCESS",
                "CUSTOMER_NUDGE": "FAILURE",
                "STOP": "SKIPPED",
            },
            ground_truth_actions=["RETRY_AFTER"],
            has_useful_memory=True,
            instruments=[{"alias": "card_5678", "type": "card", "status": "active"}],
        ))

        # Scenario 3 — Stale memory trap (HERO SCENARIO)
        scenarios.append(EvalScenario(
            id="curated_003",
            name="Stale Card Trap",
            category="method_replacement",
            customer_id="CUST-1042",
            merchant_id="MERCH-001",
            amount=8000_00,
            method="card",
            instrument_id="card_9988",
            failure_code="issuer_unavailable",
            failure_reason="Issuer temporarily unavailable",
            history=[
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_003a",
                    customer_id="CUST-1042", merchant_id="MERCH-001",
                    amount=5000_00, method="card", instrument_id="card_1234",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    timestamp=_ts(16),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_003b",
                    customer_id="CUST-1042", merchant_id="MERCH-001",
                    amount=5000_00, method="card", instrument_id="card_1234",
                    outcome="SUCCESS", action_taken="RETRY_AFTER",
                    retry_after_seconds=480, timestamp=_ts(16, hours_ago=-0.14),
                ),
                ScenarioHistory(
                    event_type="failure", payment_id="pay_hist_003c",
                    customer_id="CUST-1042", merchant_id="MERCH-001",
                    amount=6000_00, method="card", instrument_id="card_1234",
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    timestamp=_ts(9),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_003d",
                    customer_id="CUST-1042", merchant_id="MERCH-001",
                    amount=6000_00, method="card", instrument_id="card_1234",
                    outcome="SUCCESS", action_taken="RETRY_AFTER",
                    retry_after_seconds=600, timestamp=_ts(9, hours_ago=-0.17),
                ),
                # card_9988 added, supersedes card_1234
                ScenarioHistory(
                    event_type="instrument_added", payment_id="",
                    customer_id="CUST-1042", merchant_id="MERCH-001",
                    amount=0, method="card", instrument_id="card_9988",
                    timestamp=_ts(2),
                ),
            ],
            action_outcomes={
                "RETRY_NOW": "FAILURE",
                "RETRY_AFTER": "SUCCESS",  # but agent shouldn't use OLD card timing
                "SUGGEST_METHOD": "SUCCESS",
                "CUSTOMER_NUDGE": "SUCCESS",
                "STOP": "SKIPPED",
            },
            ground_truth_actions=["RETRY_AFTER", "SUGGEST_METHOD"],
            has_useful_memory=True,
            has_stale_memory=True,
            stale_instrument="card_1234",
            current_instrument="card_9988",
            instruments=[
                {"alias": "card_1234", "type": "card", "status": "superseded", "superseded_by": "card_9988"},
                {"alias": "card_9988", "type": "card", "status": "active", "supersedes": "card_1234"},
            ],
        ))

        # Scenario 4 — Merchant-specific pattern
        scenarios.append(EvalScenario(
            id="curated_004",
            name="Merchant Learning",
            category="merchant_specific",
            customer_id="CUST-2001",  # New customer for this merchant
            merchant_id="MERCH-002",
            amount=10000_00,
            method="card",
            instrument_id="card_4321",
            failure_code="issuer_unavailable",
            failure_reason="Issuer temporarily unavailable",
            history=[
                # Multiple OTHER customers succeeded with UPI at this merchant
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_004a",
                    customer_id="CUST-0010", merchant_id="MERCH-002",
                    amount=8000_00, method="upi", instrument_id="upi_primary",
                    outcome="SUCCESS", action_taken="SUGGEST_METHOD",
                    timestamp=_ts(5),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_004b",
                    customer_id="CUST-0020", merchant_id="MERCH-002",
                    amount=12000_00, method="upi", instrument_id="upi_secondary",
                    outcome="SUCCESS", action_taken="SUGGEST_METHOD",
                    timestamp=_ts(3),
                ),
                ScenarioHistory(
                    event_type="success", payment_id="pay_hist_004c",
                    customer_id="CUST-0030", merchant_id="MERCH-002",
                    amount=6000_00, method="upi", instrument_id="upi_work",
                    outcome="SUCCESS", action_taken="SUGGEST_METHOD",
                    timestamp=_ts(1),
                ),
            ],
            action_outcomes={
                "RETRY_NOW": "FAILURE",
                "RETRY_AFTER": "FAILURE",
                "SUGGEST_METHOD": "SUCCESS",
                "CUSTOMER_NUDGE": "SUCCESS",
                "STOP": "SKIPPED",
            },
            ground_truth_actions=["SUGGEST_METHOD"],
            has_useful_memory=True,
            instruments=[{"alias": "card_4321", "type": "card", "status": "active"}],
        ))

        # Scenario 5 — No memory control
        scenarios.append(EvalScenario(
            id="curated_005",
            name="No Memory Control",
            category="no_history_control",
            customer_id="CUST-9999",  # Brand new customer
            merchant_id="MERCH-099",  # Brand new merchant
            amount=3000_00,
            method="card",
            instrument_id="card_7777",
            failure_code="issuer_unavailable",
            failure_reason="Issuer temporarily unavailable",
            history=[],  # No history at all
            action_outcomes={
                "RETRY_NOW": "FAILURE",
                "RETRY_AFTER": "SUCCESS",
                "SUGGEST_METHOD": "SUCCESS",
                "CUSTOMER_NUDGE": "SUCCESS",
                "STOP": "SKIPPED",
            },
            ground_truth_actions=["RETRY_AFTER", "CUSTOMER_NUDGE", "SUGGEST_METHOD"],
            has_useful_memory=False,
            instruments=[{"alias": "card_7777", "type": "card", "status": "active"}],
        ))

        return scenarios

    def _generate_scenario(self, category: str, index: int) -> EvalScenario | None:
        """Generate a synthetic scenario for a category."""
        customer_id = self.rng.choice(CUSTOMERS)
        merchant_id = self.rng.choice(MERCHANTS)
        amount = self.rng.choice(AMOUNTS)
        method = self.rng.choice(METHODS)
        instrument_alias = self.rng.choice(INSTRUMENTS[method])

        gen_map = {
            "transient_issuer": self._gen_transient,
            "expired_instrument": self._gen_expired_instrument,
            "insufficient_balance": self._gen_insufficient_balance,
            "route_degradation": self._gen_route_degradation,
            "method_replacement": self._gen_method_replacement,
            "merchant_specific": self._gen_merchant_specific,
            "customer_specific": self._gen_customer_specific,
            "conflicting_history": self._gen_conflicting,
            "no_history_control": self._gen_no_history,
            "retry_limit_exhaustion": self._gen_retry_exhaustion,
            "successful_alternative": self._gen_successful_alternative,
            "failed_alternative": self._gen_failed_alternative,
        }

        fn = gen_map.get(category)
        if not fn:
            return None

        return fn(
            sid=f"syn_{category}_{index:03d}",
            customer_id=customer_id,
            merchant_id=merchant_id,
            amount=amount,
            method=method,
            instrument_alias=instrument_alias,
        )

    def _gen_transient(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        code, reason = self.rng.choice(TRANSIENT_CODES)
        retry_secs = self.rng.choice([300, 480, 600, 720])

        history = []
        # Previous transient + successful retry
        if self.rng.random() > 0.3:
            history.append(ScenarioHistory(
                event_type="failure", payment_id=f"pay_h_{sid}_1",
                customer_id=customer_id, merchant_id=merchant_id,
                amount=amount, method=method, instrument_id=instrument_alias,
                failure_code=code, outcome="FAILURE", timestamp=_ts(7),
            ))
            history.append(ScenarioHistory(
                event_type="success", payment_id=f"pay_h_{sid}_2",
                customer_id=customer_id, merchant_id=merchant_id,
                amount=amount, method=method, instrument_id=instrument_alias,
                outcome="SUCCESS", action_taken="RETRY_AFTER",
                retry_after_seconds=retry_secs, timestamp=_ts(7, hours_ago=-retry_secs / 3600),
            ))

        return EvalScenario(
            id=sid, name=f"Transient {code}", category="transient_issuer",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code=code, failure_reason=reason, history=history,
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "SUCCESS", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            ground_truth_actions=["RETRY_AFTER"],
            has_useful_memory=bool(history),
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_expired_instrument(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        return EvalScenario(
            id=sid, name="Expired instrument", category="expired_instrument",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="expired_card", failure_reason="Card has expired",
            history=[],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS", "STOP": "SKIPPED"},
            ground_truth_actions=["SUGGEST_METHOD"],
            has_useful_memory=False,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_insufficient_balance(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        return EvalScenario(
            id=sid, name="Insufficient balance", category="insufficient_balance",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="insufficient_funds", failure_reason="Insufficient balance",
            history=[],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS", "STOP": "SKIPPED"},
            ground_truth_actions=["SUGGEST_METHOD", "CUSTOMER_NUDGE"],
            has_useful_memory=False,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_route_degradation(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        return EvalScenario(
            id=sid, name="Route degradation", category="route_degradation",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="route_degraded", failure_reason="Temporary route degradation",
            history=[],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "SUCCESS", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            ground_truth_actions=["RETRY_AFTER"],
            has_useful_memory=False,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_method_replacement(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        alt_method = self.rng.choice([m for m in METHODS if m != method])
        new_alias = self.rng.choice(INSTRUMENTS[alt_method])
        old_alias = instrument_alias
        return EvalScenario(
            id=sid, name="Method replacement", category="method_replacement",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=new_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=[
                ScenarioHistory(
                    event_type="failure", payment_id=f"pay_h_{sid}_1",
                    customer_id=customer_id, merchant_id=merchant_id,
                    amount=amount, method=method, instrument_id=old_alias,
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    timestamp=_ts(10),
                ),
                ScenarioHistory(
                    event_type="instrument_added", payment_id="",
                    customer_id=customer_id, merchant_id=merchant_id,
                    amount=0, method=alt_method, instrument_id=new_alias,
                    timestamp=_ts(3),
                ),
            ],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "SUCCESS", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS", "STOP": "SKIPPED"},
            ground_truth_actions=["SUGGEST_METHOD", "RETRY_AFTER"],
            has_useful_memory=True,
            has_stale_memory=True,
            stale_instrument=old_alias,
            current_instrument=new_alias,
            instruments=[
                {"alias": old_alias, "type": method, "status": "superseded", "superseded_by": new_alias},
                {"alias": new_alias, "type": alt_method, "status": "active", "supersedes": old_alias},
            ],
        )

    def _gen_merchant_specific(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        other_method = self.rng.choice([m for m in METHODS if m != method])
        history = [
            ScenarioHistory(
                event_type="success", payment_id=f"pay_h_{sid}_{i}",
                customer_id=f"CUST-{self.rng.randint(100, 900):04d}",
                merchant_id=merchant_id,
                amount=self.rng.choice(AMOUNTS), method=other_method,
                instrument_id=self.rng.choice(INSTRUMENTS[other_method]),
                outcome="SUCCESS", action_taken="SUGGEST_METHOD",
                timestamp=_ts(self.rng.randint(1, 14)),
            )
            for i in range(self.rng.randint(2, 4))
        ]
        return EvalScenario(
            id=sid, name="Merchant pattern", category="merchant_specific",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=history,
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS", "STOP": "SKIPPED"},
            ground_truth_actions=["SUGGEST_METHOD"],
            has_useful_memory=True,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_customer_specific(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        retry_secs = self.rng.choice([300, 480, 600])
        history = [
            ScenarioHistory(
                event_type="success", payment_id=f"pay_h_{sid}_s",
                customer_id=customer_id, merchant_id=merchant_id,
                amount=amount, method=method, instrument_id=instrument_alias,
                outcome="SUCCESS", action_taken="RETRY_AFTER",
                retry_after_seconds=retry_secs, timestamp=_ts(5),
            )
        ]
        return EvalScenario(
            id=sid, name="Customer pattern", category="customer_specific",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=history,
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "SUCCESS", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            ground_truth_actions=["RETRY_AFTER"],
            has_useful_memory=True,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_conflicting(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        return EvalScenario(
            id=sid, name="Conflicting history", category="conflicting_history",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=[
                ScenarioHistory(
                    event_type="failure", payment_id=f"pay_h_{sid}_1",
                    customer_id=customer_id, merchant_id=merchant_id,
                    amount=amount, method=method, instrument_id=instrument_alias,
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    action_taken="RETRY_AFTER", retry_after_seconds=300, timestamp=_ts(5),
                ),
                ScenarioHistory(
                    event_type="success", payment_id=f"pay_h_{sid}_2",
                    customer_id=customer_id, merchant_id=merchant_id,
                    amount=amount, method=method, instrument_id=instrument_alias,
                    outcome="SUCCESS", action_taken="RETRY_AFTER",
                    retry_after_seconds=600, timestamp=_ts(3),
                ),
            ],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "SUCCESS", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            ground_truth_actions=["RETRY_AFTER"],
            has_useful_memory=True,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_no_history(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        cust = f"CUST-{self.rng.randint(9000, 9999):04d}"  # Guaranteed new
        merch = f"MERCH-{self.rng.randint(90, 99):03d}"  # Guaranteed new
        return EvalScenario(
            id=sid, name="No history", category="no_history_control",
            customer_id=cust, merchant_id=merch,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=[],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "SUCCESS", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS", "STOP": "SKIPPED"},
            ground_truth_actions=["RETRY_AFTER", "SUGGEST_METHOD", "CUSTOMER_NUDGE"],
            has_useful_memory=False,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_retry_exhaustion(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        return EvalScenario(
            id=sid, name="Retry exhaustion", category="retry_limit_exhaustion",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=[
                ScenarioHistory(
                    event_type="failure", payment_id=f"pay_h_{sid}_{i}",
                    customer_id=customer_id, merchant_id=merchant_id,
                    amount=amount, method=method, instrument_id=instrument_alias,
                    failure_code="issuer_unavailable", outcome="FAILURE",
                    action_taken="RETRY_AFTER", timestamp=_ts(0.5 - i * 0.1),
                )
                for i in range(3)
            ],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "FAILURE", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            ground_truth_actions=["STOP"],
            has_useful_memory=True,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_successful_alternative(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        alt_method = self.rng.choice([m for m in METHODS if m != method])
        history = [
            ScenarioHistory(
                event_type="success", payment_id=f"pay_h_{sid}_s",
                customer_id=customer_id, merchant_id=merchant_id,
                amount=amount, method=alt_method,
                instrument_id=self.rng.choice(INSTRUMENTS[alt_method]),
                outcome="SUCCESS", action_taken="SUGGEST_METHOD",
                timestamp=_ts(4),
            )
        ]
        return EvalScenario(
            id=sid, name="Successful alternative", category="successful_alternative",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=history,
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "SUCCESS", "CUSTOMER_NUDGE": "SUCCESS", "STOP": "SKIPPED"},
            ground_truth_actions=["SUGGEST_METHOD"],
            has_useful_memory=True,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )

    def _gen_failed_alternative(self, sid, customer_id, merchant_id, amount, method, instrument_alias) -> EvalScenario:
        return EvalScenario(
            id=sid, name="Failed alternative", category="failed_alternative",
            customer_id=customer_id, merchant_id=merchant_id,
            amount=amount, method=method, instrument_id=instrument_alias,
            failure_code="issuer_unavailable", failure_reason="Issuer unavailable",
            history=[],
            action_outcomes={"RETRY_NOW": "FAILURE", "RETRY_AFTER": "FAILURE", "SUGGEST_METHOD": "FAILURE", "CUSTOMER_NUDGE": "FAILURE", "STOP": "SKIPPED"},
            ground_truth_actions=["STOP", "CUSTOMER_NUDGE"],
            has_useful_memory=False,
            instruments=[{"alias": instrument_alias, "type": method, "status": "active"}],
        )
