"""End-to-end invariants for execution, shadow, batch, handoff, and policy."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph

from app.api.policies import PolicyUpdate, update_policy
from app.config import Settings
from app.domain.enums import RecoveryAction
from app.domain.models import MerchantPolicy, NormalizedPaymentEvent, RecoveryDecision
from app.evaluation.shadow import run_authority_shadow
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.batch import run_curated_batch
from app.recovery.execution_provider import RazorpayTestExecutionProvider, RecoveryExecutionProvider
from app.recovery.handoff import N8nEscalationHandoff
from app.recovery.orchestrator import RecoveryOrchestrator


class FakeExecutionProvider(RecoveryExecutionProvider):
    name = "razorpay_test"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def configured(self) -> bool:
        return True

    def create(self, execution):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider failure with secret_should_not_leak")
        return execution.model_copy(update={
            "status": "PENDING", "provider_execution_id": f"plink_test_{self.calls}",
            "public_url": f"https://rzp.io/i/test{self.calls}", "provider_status": "created",
        })


def setup(tmp_path, *, provider=None, settings=None, handoff=None):
    graph = MemoryGraph(db_path=str(tmp_path / "waggle.db"), embedding_model=EmbeddingModel("fake"))
    adapter = WaggleRecoveryMemoryAdapter(graph.for_tenant("product-tests"))
    db = Database(tmp_path / "app.db")
    orchestrator = RecoveryOrchestrator(
        adapter=adapter, db=db, execution_provider=provider,
        settings=settings or Settings(waggle_embedding_model="fake"), escalation_handoff=handoff,
    )
    return orchestrator, adapter, db


def event(**updates):
    data = {
        "event_type": "payment.failed", "payment_id": "pay_failed_product",
        "customer_id": "CUST-PRODUCT", "merchant_id": "MERCH-PRODUCT",
        "amount": 125000, "currency": "INR", "method": "card",
        "instrument_id": "card_4242", "error_code": "expired_card",
        "error_description": "Card expired", "created_at": datetime.now(UTC),
        "source": "razorpay", "test_mode": True,
    }
    data.update(updates)
    return NormalizedPaymentEvent(**data)


def test_payment_link_is_pending_idempotent_and_capture_is_authoritative(tmp_path):
    provider = FakeExecutionProvider()
    orchestrator, _, db = setup(tmp_path, provider=provider)
    failed = orchestrator.process_event(event(), simulate=False)
    duplicate = orchestrator.process_event(event(), simulate=False)

    assert failed["decision"]["action"] == "SUGGEST_METHOD"
    assert failed["outcome"]["recovered_amount"] == 0
    assert failed["execution"]["status"] == "PENDING"
    assert failed["execution"]["money_movement_confirmed"] is False
    assert duplicate["execution"]["provider_execution_id"] == failed["execution"]["provider_execution_id"]
    assert provider.calls == 1

    execution = db.get_execution_for_episode(failed["recovery_episode"]["id"])
    captured = orchestrator.process_event(event(
        event_type="payment.captured", payment_id="pay_captured_new",
        error_code="", error_description="", recovery_execution_id=execution["id"],
    ), simulate=False)
    assert captured["updated_attempts"] == 1
    assert captured["recovery_episode_id"] == failed["recovery_episode"]["id"]
    assert captured["confirmation"] == "CONFIRMED BY RAZORPAY WEBHOOK"
    assert db.get_execution_for_episode(failed["recovery_episode"]["id"])["status"] == "SUCCESS"


def test_unrelated_capture_and_provider_error_never_fabricate_success(tmp_path):
    orchestrator, _, db = setup(tmp_path, provider=FakeExecutionProvider(fail=True))
    failed = orchestrator.process_event(event(), simulate=False)
    assert failed["execution"]["status"] == "FAILED"
    assert failed["outcome"]["recovered_amount"] == 0
    unmatched = orchestrator.process_event(event(
        event_type="payment.captured", payment_id="pay_unrelated", error_code="", error_description="",
    ), simulate=False)
    assert unmatched["updated_attempts"] == 0
    assert unmatched["recovered_amount"] == 0
    assert db.get_execution_for_episode(failed["recovery_episode"]["id"])["status"] == "FAILED"


@pytest.mark.parametrize("action", [RecoveryAction.STOP, RecoveryAction.ESCALATE])
def test_terminal_actions_never_create_payment_links(tmp_path, action):
    provider = FakeExecutionProvider()
    orchestrator, _, _ = setup(tmp_path, provider=provider)

    class TerminalProvider:
        mode = "test"

        def decide_with_trace(self, bundle):
            return RecoveryDecision(
                failure_id=bundle.current_failure.id, action=action, reason="terminal", confidence=1,
            ), {"decision_mode": "test"}

    result = orchestrator.process_event(event(payment_id=f"pay_{action.value}"), simulate=False, decision_provider=TerminalProvider())
    assert result["execution"] is None
    assert result["outcome"]["recovered_amount"] == 0
    assert provider.calls == 0


def test_policy_block_never_creates_payment_link(tmp_path):
    provider = FakeExecutionProvider()
    orchestrator, _, _ = setup(tmp_path, provider=provider)

    class SuggestProvider:
        mode = "test"

        def decide_with_trace(self, bundle):
            return RecoveryDecision(
                failure_id=bundle.current_failure.id, action=RecoveryAction.SUGGEST_METHOD,
                recommended_method="upi", reason="candidate", confidence=.9,
            ), {"decision_mode": "test"}

    policy = MerchantPolicy(
        merchant_id="MERCH-PRODUCT", allowed_actions=[RecoveryAction.SUGGEST_METHOD, RecoveryAction.STOP],
        blocked_methods=["upi"],
    )
    result = orchestrator.process_event(event(), merchant_policy=policy, simulate=False, decision_provider=SuggestProvider())
    assert result["decision"]["policy_result"] == "BLOCK"
    assert result["decision"]["action"] == "ESCALATE"
    assert result["execution"] is None
    assert provider.calls == 0


def test_razorpay_provider_uses_official_contract_and_never_exposes_secrets():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"id": "plink_test_contract", "short_url": "https://rzp.io/i/demo", "status": "created"})

    settings = Settings(
        razorpay_enabled=True, razorpay_test_execution_enabled=True,
        razorpay_key_id="rzp_test_public", razorpay_key_secret="super-secret-key",
        razorpay_webhook_secret="webhook-secret",
    )
    provider = RazorpayTestExecutionProvider(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    from app.domain.models import RecoveryExecution
    created = provider.create(RecoveryExecution(
        provider="razorpay_test", recovery_episode_id="rep_1", failure_id="fail_1", decision_id="dec_1",
        attempt_id="att_1", merchant_id="merch_1", customer_id="cust_1", amount=1000,
    ))
    payload = json.loads(seen["request"].content)
    assert seen["request"].method == "POST"
    assert seen["request"].url.path == "/v1/payment_links"
    assert payload["amount"] == 1000 and payload["currency"] == "INR"
    assert payload["accept_partial"] is False
    assert payload["notes"]["recovery_episode_id"] == "rep_1"
    assert created.status == "PENDING"
    assert "super-secret-key" not in json.dumps(created.safe_dict())


def test_shadow_comparison_is_isolated_and_cached_metrics_are_not_marketing_copy():
    result = run_authority_shadow("curated_003")
    assert result["persisted_as_recovery_attempt"] is False
    assert result["without_authority_validation"]["known_stale_evidence_influenced_action"] is True
    assert result["with_authority_validation"]["known_stale_evidence_influenced_action"] is False
    assert result["diff"]["evidence_removed_count"] > 0


def test_batch_uses_normal_pipeline_and_keeps_money_classes_separate(tmp_path):
    orchestrator, _, db = setup(tmp_path)
    result = run_curated_batch(orchestrator, db, count=20)
    assert result["case_count"] == 20
    assert len(result["cases"]) == 20
    assert result["total_gmv_at_risk"] > 0
    assert result["pending_test_mode_gmv"] == 0
    assert result["confirmed_test_mode_recovered_gmv"] == 0
    assert result["unsafe_action_count"] == 0
    assert result["policy_violation_count"] == 0


def test_n8n_handoff_is_signed_minimal_and_failure_cannot_change_escalation(tmp_path):
    received = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        received["count"] += 1
        received["body"] = json.loads(request.content)
        received["signature"] = request.headers["x-waggle-signature"]
        return httpx.Response(503)

    settings = Settings(
        waggle_embedding_model="fake", n8n_enabled=True,
        n8n_escalation_webhook_url="https://n8n.example.test/webhook/recovery",
        n8n_webhook_secret="n8n-secret",
    )
    handoff = N8nEscalationHandoff(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    orchestrator, _, db = setup(tmp_path, settings=settings, handoff=handoff)

    class EscalateProvider:
        mode = "test"

        def decide_with_trace(self, bundle):
            return RecoveryDecision(
                failure_id=bundle.current_failure.id, action=RecoveryAction.ESCALATE,
                reason="review", abstention_reason="review", confidence=1,
            ), {"decision_mode": "test"}

    result = orchestrator.process_event(event(), simulate=False, decision_provider=EscalateProvider())
    escalation = db.get_escalation_for_episode(result["recovery_episode"]["id"])
    assert result["decision"]["action"] == "ESCALATE"
    assert result["outcome"]["recovered_amount"] == 0
    assert escalation["external_workflow_status"] == "FAILED"
    assert received["body"]["final_action"] == "ESCALATE"
    assert received["body"]["money_movement"] == "NONE"
    assert "n8n-secret" not in json.dumps(received["body"])
    assert received["signature"]

    replay = orchestrator.process_event(event(), simulate=False, decision_provider=EscalateProvider())
    assert replay["status"] == "terminal"
    assert received["count"] == 1

    class StopProvider:
        mode = "test"

        def decide_with_trace(self, bundle):
            return RecoveryDecision(
                failure_id=bundle.current_failure.id, action=RecoveryAction.STOP,
                reason="stop", confidence=1,
            ), {"decision_mode": "test"}

    class NormalProvider:
        mode = "test"

        def decide_with_trace(self, bundle):
            return RecoveryDecision(
                failure_id=bundle.current_failure.id, action=RecoveryAction.SUGGEST_METHOD,
                recommended_method="upi", reason="normal", confidence=1,
            ), {"decision_mode": "test"}

    stopped = orchestrator.process_event(
        event(payment_id="pay_n8n_stop"), simulate=False, decision_provider=StopProvider(),
    )
    normal = orchestrator.process_event(
        event(payment_id="pay_n8n_normal"), simulate=False, decision_provider=NormalProvider(),
    )
    assert stopped["decision"]["action"] == "STOP"
    assert normal["decision"]["action"] == "SUGGEST_METHOD"
    assert received["count"] == 1


def test_policy_console_creates_immutable_versions_and_cannot_restart_terminal_episode(tmp_path):
    orchestrator, adapter, _ = setup(tmp_path)
    first = asyncio.run(update_policy(
        "MERCH-POLICY-UI", PolicyUpdate(max_recovery_attempts=3),
        adapter=adapter, orchestrator=orchestrator, _authorized=None,
    ))
    second = asyncio.run(update_policy(
        "MERCH-POLICY-UI", PolicyUpdate(
            max_recovery_attempts=2, blocked_methods=["card"],
            allowed_actions=[RecoveryAction.SUGGEST_METHOD, RecoveryAction.STOP],
            min_automatic_confidence=.7,
        ),
        adapter=adapter, orchestrator=orchestrator, _authorized=None,
    ))
    assert first["current"]["version"] == 1
    assert second["current"]["version"] == 2
    assert second["current"]["supersedes_policy_id"] == first["current"]["policy_id"]
    assert [item["version"] for item in second["history"]] == [2, 1]
    assert second["history"][0]["current"] is True
    assert second["history"][1]["current"] is False
    assert orchestrator._load_merchant_policy("MERCH-POLICY-UI").version == 2

    class StopProvider:
        mode = "test"

        def decide_with_trace(self, bundle):
            return RecoveryDecision(
                failure_id=bundle.current_failure.id, action=RecoveryAction.STOP,
                reason="terminal", confidence=1,
            ), {"decision_mode": "test"}

    terminal_event = event(payment_id="pay_policy_terminal", merchant_id="MERCH-POLICY-UI")
    stopped = orchestrator.process_event(terminal_event, simulate=True, decision_provider=StopProvider())
    asyncio.run(update_policy(
        "MERCH-POLICY-UI", PolicyUpdate(blocked_methods=[]),
        adapter=adapter, orchestrator=orchestrator, _authorized=None,
    ))
    replay = orchestrator.process_event(terminal_event, simulate=True)
    assert stopped["decision"]["action"] == "STOP"
    assert replay["status"] == "terminal"
    assert replay["terminal_state"]["action"] == "STOP"


def test_malformed_policy_is_rejected_cleanly(tmp_path):
    orchestrator, adapter, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="retry interval bounds"):
        asyncio.run(update_policy(
            "MERCH-BAD-POLICY",
            PolicyUpdate(min_retry_interval_seconds=900, max_retry_interval_seconds=300),
            adapter=adapter, orchestrator=orchestrator, _authorized=None,
        ))
