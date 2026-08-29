from app.domain.models import EvidenceBundle, PaymentFailure, PaymentInstrument
from app.recovery.risk import assess_revenue_risk


def test_risk_score_is_explainable_and_bounded():
    bundle = EvidenceBundle(
        current_failure=PaymentFailure(
            external_payment_id="pay_risk",
            customer_id="CUST-RISK",
            merchant_id="MERCH-RISK",
            amount=2_000_000,
            method="card",
            instrument_id="card_risk",
            failure_code="expired_card",
        ),
        current_instruments=[PaymentInstrument(
            customer_id="CUST-RISK",
            instrument_type="card",
            fingerprint_or_safe_alias="card_risk",
            status="active",
        )],
        retry_count=2,
    )

    risk = assess_revenue_risk(bundle)
    assert 0 <= risk.score <= 100
    assert risk.band in {"HIGH", "CRITICAL"}
    assert "+ high payment value" in risk.factors
    assert any("recovery attempt" in factor for factor in risk.factors)
