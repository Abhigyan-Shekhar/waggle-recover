"""FastAPI application — dependency injection and startup."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter
from app.persistence.database import Database
from app.recovery.decision_engine import create_decision_provider
from app.recovery.orchestrator import RecoveryOrchestrator

LOGGER = logging.getLogger(__name__)

# ── Singletons ──────────────────────────────────────────────────────────────

_db: Database | None = None
_adapter: WaggleRecoveryMemoryAdapter | None = None
_orchestrator: RecoveryOrchestrator | None = None


def _initialize(settings: Settings) -> None:
    global _db, _adapter, _orchestrator

    LOGGER.info("Initializing Waggle Recover backend...")

    from waggle.embeddings import EmbeddingModel
    from waggle.graph import MemoryGraph

    graph = MemoryGraph(
        db_path=str(settings.waggle_db_abs_path),
        embedding_model=EmbeddingModel(settings.waggle_embedding_model),
        enable_dedup=settings.waggle_enable_dedup,
    )
    tenant_graph = graph.for_tenant(settings.waggle_tenant_id)

    _adapter = WaggleRecoveryMemoryAdapter(tenant_graph)
    _db = Database(str(settings.app_db_abs_path))

    decision_provider = create_decision_provider(settings.decision_provider, settings=settings)
    _orchestrator = RecoveryOrchestrator(
        adapter=_adapter,
        db=_db,
        decision_provider=decision_provider,
        settings=settings,
    )

    LOGGER.info("Waggle Recover backend initialized.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _initialize(settings)
    yield


# ── Dependency providers ────────────────────────────────────────────────────


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def get_adapter() -> WaggleRecoveryMemoryAdapter:
    if _adapter is None:
        raise RuntimeError("Adapter not initialized")
    return _adapter


def get_orchestrator() -> RecoveryOrchestrator:
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialized")
    return _orchestrator


# ── App ──────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Waggle Recover API",
        version="0.1.0",
        description=(
            "Persistent, temporal, supersession-aware payment recovery agent "
            "on top of the Waggle memory graph."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    from app.api.payments import router as payments_router
    from app.api.webhooks import router as webhooks_router
    from app.api.decisions import router as decisions_router
    from app.api.simulator import router as simulator_router
    from app.api.evaluation import router as evaluation_router
    from app.api.memory_graph import router as memory_graph_router
    from app.api.mandate import router as mandate_router

    app.include_router(payments_router, prefix="/api/payments", tags=["payments"])
    app.include_router(webhooks_router, prefix="/api/webhooks", tags=["webhooks"])
    app.include_router(decisions_router, prefix="/api/decisions", tags=["decisions"])
    app.include_router(simulator_router, prefix="/api/simulator", tags=["simulator"])
    # Compatibility alias for early demo clients.
    app.include_router(simulator_router, prefix="/api/simulate", tags=["simulator"])
    app.include_router(evaluation_router, prefix="/api/evaluation", tags=["evaluation"])
    app.include_router(memory_graph_router, prefix="/api/memory", tags=["memory"])
    app.include_router(mandate_router, prefix="/api/mandate", tags=["mandate"])

    @app.get("/")
    async def root():
        return {
            "name": "Waggle Recover",
            "version": "0.1.0",
            "status": "running",
        }

    @app.get("/health")
    async def health():
        agent_configured = bool(settings.groq_api_key and settings.groq_model)
        return {
            "status": "ok",
            "agent": {
                "provider": "groq",
                "configured": agent_configured,
                "model": settings.groq_model or None,
                "reason": None if agent_configured else "GROQ_API_KEY is not configured",
            },
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
