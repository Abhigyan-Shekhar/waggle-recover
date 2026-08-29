import pytest

from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.orchestrator import RecoveryOrchestrator
from app.recovery.subscription_scenarios import run_subscription_scenario


@pytest.fixture
def tmp_setup(tmp_path):
    from waggle.embeddings import EmbeddingModel
    from waggle.graph import MemoryGraph

    graph = MemoryGraph(db_path=str(tmp_path / "waggle.db"), embedding_model=EmbeddingModel("fake"))
    adapter = WaggleRecoveryMemoryAdapter(graph.for_tenant("subscription-test"))
    db = Database(str(tmp_path / "app.db"))
    return RecoveryOrchestrator(adapter=adapter, db=db), adapter, db


def test_subscription_recovery_reuses_temporal_validation(tmp_setup):
    orchestrator, _, db = tmp_setup
    result = run_subscription_scenario("mandate_instrument_replaced", orchestrator, db)

    assert result["risk_type"] == "SUBSCRIPTION_FAILURE"
    assert result["result"]["recovery_episode"]["scope_type"] == "subscription"
    assert result["result"]["decision"]["action"] == "SUGGEST_METHOD"
    assert result["result"]["metrics"]["evidence_discarded"] > 0


def test_subscription_attempt_limit_creates_human_handoff(tmp_setup):
    orchestrator, _, db = tmp_setup
    result = run_subscription_scenario("mandate_escalation", orchestrator, db)["result"]

    assert result["decision"]["action"] == "ESCALATE"
    assert result["escalation"]["human_review_required"] is True
    assert result["outcome"]["recovered_amount"] == 0
