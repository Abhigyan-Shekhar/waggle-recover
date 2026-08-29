"""Regression tests for repeatable, state-isolated curated demos."""

from app.evaluation.generator import ScenarioGenerator, isolate_demo_run


def _scenario(name: str):
    return next(item for item in ScenarioGenerator(seed=42)._curated_scenarios() if item.name == name)


def test_repeated_stale_card_demos_use_distinct_customer_and_payment_scopes():
    source = _scenario("Stale Card Trap")
    first = isolate_demo_run(source, "first")
    second = isolate_demo_run(source, "second")

    assert first.customer_id != second.customer_id
    assert first.merchant_id != second.merchant_id
    assert {item.customer_id for item in first.history} != {item.customer_id for item in second.history}
    assert {item.merchant_id for item in first.history} != {item.merchant_id for item in second.history}
    assert {item.payment_id for item in first.history if item.payment_id}.isdisjoint(
        {item.payment_id for item in second.history if item.payment_id}
    )


def test_escalation_attempts_keep_one_episode_id_inside_isolated_run():
    isolated = isolate_demo_run(_scenario("Escalation Required"), "run123")

    assert isolated.current_payment_id
    assert {item.payment_id for item in isolated.history} == {isolated.current_payment_id}
