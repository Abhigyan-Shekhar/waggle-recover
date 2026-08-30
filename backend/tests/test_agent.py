"""Safety and integration tests for the constrained LangGraph recovery agent."""
from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any

import pytest
from waggle.embeddings import EmbeddingModel

from app.config import Settings
from app.domain.enums import MemoryContribution, OutcomeStatus, RecoveryAction, TemporalStatus
from app.domain.models import (
    EvidenceBundle,
    EvidenceReference,
    MerchantPolicy,
    NormalizedPaymentEvent,
    PaymentFailure,
    PaymentInstrument,
    StrategyPriorEstimate,
)
from app.evaluation.generator import ScenarioGenerator
from app.evaluation.runner import _populate_memory
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.agent import AgentDecisionProvider, GroqQwenClient
from app.recovery.decision_engine import DeterministicDecisionProvider, create_decision_provider
from app.recovery.orchestrator import RecoveryOrchestrator


class FakeModelClient:
    def __init__(self, result: str | Exception | Callable[[dict[str, Any]], str]) -> None:
        self.result = result
        self.contexts: list[dict[str, Any]] = []
        self.system_prompts: list[str] = []

    def complete(self, *, system_prompt, trusted_context, model, temperature):
        self.contexts.append(trusted_context)
        self.system_prompts.append(system_prompt)
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result(trusted_context)
        return self.result


def candidate(**updates) -> str:
    data = {
        "action": "SUGGEST_METHOD",
        "retry_after_seconds": None,
        "recommended_method": "upi",
        "confidence": 0.82,
        "reason": "Trusted memory has no authoritative retry timing.",
        "evidence_ids": [],
    }
    data.update(updates)
    return json.dumps(data)


@pytest.fixture
def evidence_bundle() -> EvidenceBundle:
    failure = PaymentFailure(
        external_payment_id="pay_agent_current",
        customer_id="CUST-AGENT",
        merchant_id="MERCH-AGENT",
        amount=800000,
        method="card",
        instrument_id="card_new",
        failure_code="issuer_unavailable",
    )
    accepted = EvidenceReference(
        waggle_node_id="accepted-1",
        label="Current card recovery success",
        memory_type="recovery_outcome",
        relevance_score=0.91,
        temporal_status=TemporalStatus.CURRENT,
        metadata={
            "action_type": "RETRY_AFTER",
            "outcome": "SUCCESS",
            "instrument_id": "card_new",
            "retry_after_seconds": 480,
            "retry_timing_scope_match": True,
        },
    )
    rejected = EvidenceReference(
        waggle_node_id="rejected-old-card",
        label="Old card recovery success",
        memory_type="recovery_outcome",
        relevance_score=0.95,
        temporal_status=TemporalStatus.SUPERSEDED,
        accepted=False,
        rejection_reason="card_old was superseded by card_new",
        metadata={
            "action_type": "RETRY_AFTER",
            "outcome": "SUCCESS",
            "instrument_id": "card_old",
            "retry_after_seconds": 600,
        },
    )
    return EvidenceBundle(
        current_failure=failure,
        accepted_evidence=[accepted],
        discarded_evidence=[rejected],
        current_instruments=[
            PaymentInstrument(customer_id="CUST-AGENT", instrument_type="card", fingerprint_or_safe_alias="card_new")
        ],
        merchant_policy=MerchantPolicy(merchant_id="MERCH-AGENT"),
        memory_contribution=MemoryContribution.FULL_CONTEXT,
    )


def test_agent_receives_only_accepted_evidence_as_usable_memory(evidence_bundle):
    evidence_bundle.strategy_priors = [StrategyPriorEstimate(
        action=RecoveryAction.RETRY_AFTER,
        recommended_method="card",
        posterior_success_probability=0.74,
        global_prior=0.61,
        weighted_successes=8.2,
        weighted_failures=2.8,
        effective_n=11.0,
        insufficient_history=False,
        selected_bucket="merchant_failure_code_method_strategy",
        authoritative_evidence_ids=["accepted-1"],
        excluded_stale_evidence_ids=["rejected-old-card"],
    )]
    client = FakeModelClient(candidate(action="RETRY_AFTER", retry_after_seconds=480,
                                       recommended_method="card", evidence_ids=["accepted-1"]))
    decision, trace = AgentDecisionProvider(model="test-qwen", model_client=client).decide_with_trace(evidence_bundle)

    context = client.contexts[0]
    assert context["trusted_historical_evidence"][0]["evidence_id"] == "accepted-1"
    assert context["trusted_historical_evidence"][0]["usable_as_evidence"] is True
    assert context["rejected_memory_summary"] == {"count": 1, "categories": ["SUPERSEDED"]}
    assert "rejected_memory_for_transparency_only" not in context
    assert "rejected-old-card" not in json.dumps(context)
    assert "card_old" not in json.dumps(context)
    assert all(item.get("retry_after_seconds") != 600 for item in context["trusted_historical_evidence"])
    assert context["safe_alternative_methods"][0] == "upi"
    assert "card" not in context["safe_alternative_methods"]
    assert context["authoritative_strategy_priors"][0]["posterior_success_probability"] == 0.74
    assert "rejected-old-card" not in context["authoritative_strategy_priors"][0]["authoritative_evidence_ids"]
    assert "not evidence" in client.system_prompts[0].lower()
    assert "prefer suggest_method" in client.system_prompts[0].lower()
    assert decision.evidence_references[0].waggle_node_id == "accepted-1"
    assert trace["agent_fallback"] is False
    assert trace["authoritative_strategy_priors"][0]["effective_n"] == 11.0


def test_agent_cannot_cite_rejected_evidence(evidence_bundle):
    client = FakeModelClient(candidate(evidence_ids=["rejected-old-card"]))
    decision, trace = AgentDecisionProvider(model="test-qwen", model_client=client).decide_with_trace(evidence_bundle)

    assert trace["agent_fallback"] is True
    assert "Model cited rejected evidence" in trace["fallback_reason"]
    assert all(ref.waggle_node_id != "rejected-old-card" for ref in decision.evidence_references)


def test_unknown_evidence_cannot_enter_qwen_context_even_if_bundle_is_malformed(evidence_bundle):
    unknown = evidence_bundle.accepted_evidence[0].model_copy(update={
        "waggle_node_id": "unknown-1",
        "temporal_status": TemporalStatus.UNKNOWN,
    })
    malformed = evidence_bundle.model_copy(update={
        "accepted_evidence": [unknown],
        "discarded_evidence": [],
    })
    client = FakeModelClient(candidate(evidence_ids=["unknown-1"]))

    decision, trace = AgentDecisionProvider(model="test-qwen", model_client=client).decide_with_trace(malformed)

    assert client.contexts[0]["trusted_historical_evidence"] == []
    assert trace["agent_fallback"] is True
    assert "rejected evidence" in trace["fallback_reason"].lower()
    assert decision.evidence_references == []


def test_agent_normalizes_parameters_irrelevant_to_selected_action(evidence_bundle):
    raw = candidate(action="SUGGEST_METHOD", retry_after_seconds=300, recommended_method="upi")
    decision, trace = AgentDecisionProvider(
        model="test-qwen",
        model_client=FakeModelClient(raw),
    ).decide_with_trace(evidence_bundle)

    assert trace["agent_fallback"] is False
    assert decision.action == RecoveryAction.SUGGEST_METHOD
    assert decision.retry_after_seconds is None
    assert trace["candidate_retry_after_seconds"] is None


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (candidate(action="TRANSFER_MONEY"), "Action is not in RecoveryAction"),
        ("not-json", "Model returned malformed JSON"),
        (candidate(action="RETRY_AFTER", retry_after_seconds=None, recommended_method="card"),
         "RETRY_AFTER requires retry_after_seconds"),
        (candidate(recommended_method="crypto_coin"), "recommended_method is not a supported payment method"),
        (candidate(evidence_ids=["invented-node"]), "Model cited unknown evidence"),
    ],
)
def test_invalid_agent_output_uses_deterministic_fallback(evidence_bundle, raw, error):
    decision, trace = AgentDecisionProvider(model="test-qwen", model_client=FakeModelClient(raw)).decide_with_trace(evidence_bundle)
    assert trace["agent_fallback"] is True
    assert error in trace["fallback_reason"]
    assert decision.action in list(RecoveryAction)


def test_timeout_uses_deterministic_fallback(evidence_bundle):
    _, trace = AgentDecisionProvider(model="test-qwen", model_client=FakeModelClient(TimeoutError())).decide_with_trace(evidence_bundle)
    assert trace["agent_fallback"] is True
    assert trace["fallback_reason"] == "Model call timed out"


def test_missing_groq_key_reports_configuration_problem(evidence_bundle):
    provider = AgentDecisionProvider(
        model="qwen/qwen3.8-27b",
        model_client=GroqQwenClient(api_key="", timeout_seconds=1),
    )
    _, trace = provider.decide_with_trace(evidence_bundle)

    assert trace["agent_fallback"] is True
    assert trace["fallback_reason"] == "GROQ_API_KEY is not configured"
    assert "disabled until GROQ_API_KEY" in trace["stages"][2]["detail"]


def test_rate_limit_is_distinguished_from_generic_model_failure(evidence_bundle):
    rate_limit = RuntimeError("response body must not leak")
    rate_limit.status_code = 429  # type: ignore[attr-defined]
    _, trace = AgentDecisionProvider(
        model="test-qwen",
        model_client=FakeModelClient(rate_limit),
    ).decide_with_trace(evidence_bundle)

    assert trace["agent_fallback"] is True
    assert trace["fallback_reason"] == "Groq rate limit reached (HTTP 429)"
    assert "response body" not in json.dumps(trace)


def test_no_history_does_not_fabricate_evidence(evidence_bundle):
    empty = evidence_bundle.model_copy(update={
        "accepted_evidence": [],
        "discarded_evidence": [],
        "memory_contribution": MemoryContribution.NONE,
    })
    raw = candidate(action="CUSTOMER_NUDGE", recommended_method=None, evidence_ids=[],
                    reason="No trusted customer history is available.")
    decision, trace = AgentDecisionProvider(model="test-qwen", model_client=FakeModelClient(raw)).decide_with_trace(empty)
    assert decision.action == RecoveryAction.CUSTOMER_NUDGE
    assert decision.evidence_references == []
    assert trace["cited_evidence_ids"] == []


@pytest.fixture
def orchestrator_setup(tmp_path):
    from waggle.graph import MemoryGraph

    graph = MemoryGraph(db_path=str(tmp_path / "waggle.db"), embedding_model=EmbeddingModel("fake"))
    adapter = WaggleRecoveryMemoryAdapter(graph.for_tenant("agent-test"))
    db = Database(str(tmp_path / "app.db"))
    return RecoveryOrchestrator(adapter=adapter, db=db), adapter, db


def event(payment_id="pay_agent_policy") -> NormalizedPaymentEvent:
    return NormalizedPaymentEvent(
        event_type="payment.failed",
        payment_id=payment_id,
        customer_id="CUST-AGENT-POLICY",
        merchant_id="MERCH-AGENT-POLICY",
        amount=100000,
        method="card",
        instrument_id="card_policy",
        error_code="issuer_unavailable",
        error_description="Issuer unavailable",
    )


def test_policy_modifies_agent_retry_proposal(orchestrator_setup):
    orchestrator, _, _ = orchestrator_setup
    provider = AgentDecisionProvider(
        model="test-qwen",
        model_client=FakeModelClient(candidate(action="RETRY_AFTER", retry_after_seconds=60,
                                               recommended_method="card", evidence_ids=[])),
    )
    policy = MerchantPolicy(merchant_id="MERCH-AGENT-POLICY", min_retry_interval_seconds=300)
    result = orchestrator.process_event(event(), merchant_policy=policy, simulate=True, decision_provider=provider)

    assert result["agent_trace"]["candidate_retry_after_seconds"] == 60
    assert result["agent_trace"]["policy_result"] == "MODIFY"
    assert result["decision"]["retry_after_seconds"] == 300


def test_policy_blocks_agent_method_proposal(orchestrator_setup):
    orchestrator, _, _ = orchestrator_setup
    provider = AgentDecisionProvider(model="test-qwen", model_client=FakeModelClient(candidate()))
    policy = MerchantPolicy(merchant_id="MERCH-AGENT-POLICY", blocked_methods=["upi"])
    result = orchestrator.process_event(event("pay_agent_block"), merchant_policy=policy,
                                        simulate=True, decision_provider=provider)

    assert result["agent_trace"]["candidate_action"] == "SUGGEST_METHOD"
    assert result["agent_trace"]["policy_result"] == "BLOCK"
    assert result["decision"]["action"] == "ESCALATE"
    assert result["decision"]["recommended_method"] is None
    assert result["decision"]["human_review_required"] is True


def test_stale_card_trap_agent_cannot_use_old_timing(orchestrator_setup):
    orchestrator, adapter, db = orchestrator_setup
    scenario = next(item for item in ScenarioGenerator(seed=42)._curated_scenarios() if item.name == "Stale Card Trap")
    _populate_memory(adapter, db, orchestrator, scenario)
    client = FakeModelClient(candidate())
    provider = AgentDecisionProvider(model="test-qwen", model_client=client)

    result = orchestrator.process_event(
        event=NormalizedPaymentEvent(
            event_type="payment.failed",
            payment_id="pay_agent_stale",
            customer_id=scenario.customer_id,
            merchant_id=scenario.merchant_id,
            amount=scenario.amount,
            method=scenario.method,
            instrument_id=scenario.instrument_id,
            error_code=scenario.failure_code,
            error_description=scenario.failure_reason,
        ),
        simulation_outcomes=scenario.action_outcomes,
        simulate=True,
        decision_provider=provider,
    )

    assert result["decision"]["action"] == "SUGGEST_METHOD"
    assert result["agent_trace"]["rejected_evidence_ids"]
    assert result["agent_trace"]["cited_evidence_ids"] == []
    assert client.contexts[0]["trusted_historical_evidence"] == []
    assert client.contexts[0]["rejected_memory_summary"]["count"] > 0
    assert "card_legacy" not in json.dumps(client.contexts[0])


def test_timing_memory_agent_can_use_valid_interval(orchestrator_setup):
    orchestrator, adapter, db = orchestrator_setup
    scenario = next(item for item in ScenarioGenerator(seed=42)._curated_scenarios() if item.name == "Timing Memory")
    _populate_memory(adapter, db, orchestrator, scenario)

    def timing_response(context):
        trusted = next(item for item in context["trusted_historical_evidence"] if item["retry_after_seconds"])
        return candidate(action="RETRY_AFTER", retry_after_seconds=trusted["retry_after_seconds"],
                         recommended_method="card", evidence_ids=[trusted["evidence_id"]],
                         reason="Trusted timing memory matches this exact payment scope.")

    provider = AgentDecisionProvider(model="test-qwen", model_client=FakeModelClient(timing_response))
    result = orchestrator.process_event(
        event=NormalizedPaymentEvent(
            event_type="payment.failed",
            payment_id="pay_agent_timing",
            customer_id=scenario.customer_id,
            merchant_id=scenario.merchant_id,
            amount=scenario.amount,
            method=scenario.method,
            instrument_id=scenario.instrument_id,
            error_code=scenario.failure_code,
            error_description=scenario.failure_reason,
        ),
        simulation_outcomes=scenario.action_outcomes,
        simulate=True,
        decision_provider=provider,
    )

    assert result["decision"]["action"] == "RETRY_AFTER"
    assert result["decision"]["retry_after_seconds"] in (480, 600)
    assert result["agent_trace"]["cited_evidence_ids"]
    assert result["outcome"]["outcome"] == OutcomeStatus.SUCCESS


def test_trace_is_structured_and_does_not_leak_secret_or_chain_of_thought(evidence_bundle):
    secret = "gsk_do_not_leak_this"
    provider = AgentDecisionProvider(api_key=secret, model="test-qwen", model_client=FakeModelClient(candidate()))
    _, trace = provider.decide_with_trace(evidence_bundle)
    serialized = json.dumps(trace)
    assert secret not in serialized
    assert "prompt" not in trace
    assert "chain_of_thought" not in serialized
    assert [stage["key"] for stage in trace["stages"]] == [
        "semantic_memory", "temporal_validation", "agent_reasoning"
    ]


def test_factory_preserves_deterministic_default_and_builds_agent():
    deterministic = create_decision_provider("deterministic")
    assert isinstance(deterministic, DeterministicDecisionProvider)
    assert deterministic.mode == "deterministic"
    settings = Settings(groq_model="test-qwen")
    assert isinstance(create_decision_provider("agent", settings=settings,
                                               model_client=FakeModelClient(candidate())), AgentDecisionProvider)


def test_simulator_can_select_agent_mode_without_changing_default_provider():
    importlib.import_module("app.main")
    from app.api.simulator import _simulator_decision_provider

    settings = Settings(groq_model="test-qwen")
    assert isinstance(_simulator_decision_provider("agent", settings), AgentDecisionProvider)
    assert isinstance(_simulator_decision_provider("deterministic", settings), DeterministicDecisionProvider)


@pytest.mark.asyncio
async def test_real_webhook_cannot_be_overridden_by_simulator_decision_mode():
    importlib.import_module("app.main")
    from app.api.webhooks import razorpay_webhook

    payload = {
        "event": "payment.failed",
        "decision_mode": "agent",
        "payload": {"payment": {"entity": {
            "id": "pay_webhook_mode_guard",
            "amount": 100000,
            "currency": "INR",
            "method": "card",
            "card": {"last4": "1234"},
            "notes": {"customer_id": "CUST-WEBHOOK", "decision_mode": "agent"},
            "merchant_id": "MERCH-WEBHOOK",
            "error_code": "issuer_unavailable",
            "error_description": "Issuer unavailable",
            "created_at": 1_700_000_000,
        }}},
    }

    class RequestStub:
        async def body(self):
            return json.dumps(payload).encode()

    class DatabaseStub:
        def upsert_webhook_event(self, record):
            return True

        def mark_webhook_processed(self, event_id):
            return None

    class OrchestratorSpy:
        def __init__(self):
            self.kwargs = None

        def process_event(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "processed"}

    orchestrator = OrchestratorSpy()
    await razorpay_webhook(
        request=RequestStub(),
        x_razorpay_signature=None,
        db=DatabaseStub(),
        orchestrator=orchestrator,
        settings=Settings(razorpay_enabled=False),
    )

    assert orchestrator.kwargs["simulate"] is False
    assert "decision_provider" not in orchestrator.kwargs
    assert orchestrator.kwargs["event"].source == "razorpay"
