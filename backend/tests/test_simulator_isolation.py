"""Regression tests for repeatable, state-isolated curated demos."""

import app.main  # noqa: F401  # Initialize router imports in application order.
from app.api.simulator import _isolate_demo_run
from app.evaluation.generator import ScenarioGenerator


def _scenario(name: str):
    return next(item for item in ScenarioGenerator(seed=42)._curated_scenarios() if item.name == name)


def test_repeated_stale_card_demos_use_distinct_customer_and_payment_scopes():
    source = _scenario("Stale Card Trap")
    first = _isolate_demo_run(source, "first")
    second = _isolate_demo_run(source, "second")

    assert first.customer_id != second.customer_id
    assert {item.customer_id for item in first.history} != {item.customer_id for item in second.history}
    assert {item.payment_id for item in first.history if item.payment_id}.isdisjoint(
        {item.payment_id for item in second.history if item.payment_id}
    )


def test_escalation_attempts_keep_one_episode_id_inside_isolated_run():
    isolated = _isolate_demo_run(_scenario("Escalation Required"), "run123")

    assert isolated.current_payment_id
    assert {item.payment_id for item in isolated.history} == {isolated.current_payment_id}
