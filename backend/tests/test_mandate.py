from app.domain.enums import RecoveryAction
from app.domain.models import MandateContext
from app.recovery.mandate import recommend_mandate_recovery


def test_mandate_does_not_claim_rail_control():
    result = recommend_mandate_recovery(MandateContext(mandate_id="m_1", customer_id="c_1", merchant_id="merch_1", amount=1000))
    assert result["action"] == RecoveryAction.WAIT_NEXT_CYCLE.value
    assert result["rail_control"] == "none"


def test_mandate_stops_after_repeated_failures():
    result = recommend_mandate_recovery(MandateContext(mandate_id="m_2", customer_id="c_1", merchant_id="merch_1", amount=1000, previous_failures=3))
    assert result["action"] == RecoveryAction.STOP.value
