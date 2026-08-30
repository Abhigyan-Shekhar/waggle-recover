"""Configuration for Waggle Recover backend."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "Waggle Recover"
    app_version: str = "0.1.0"
    debug: bool = False
    protect_mutation_endpoints: bool = False
    mutation_api_token: str = ""

    # Database
    app_db_path: str = "./data/waggle_recover.db"
    waggle_db_path: str = "./data/waggle_memory.db"

    # Waggle memory
    waggle_tenant_id: str = "waggle-recover"
    waggle_embedding_model: str = "all-MiniLM-L6-v2"
    waggle_enable_dedup: bool = False  # Disable dedup for payment events - each is unique

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_enabled: bool = False
    webhook_replay_window_seconds: int = 86400

    # Decision engine
    decision_provider: str = "deterministic"  # deterministic / llm / agent
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    # Keep the public model choice reproducible; only the API key is secret/runtime configuration.
    groq_model: str = "qwen/qwen3.8-27b"
    agent_temperature: float = 0.0
    agent_timeout_seconds: float = 15.0
    agent_min_confidence: float = 0.60

    # Recovery thresholds
    lookup_first_confidence_threshold: float = 0.75
    max_evidence_nodes: int = 20
    evidence_recency_half_life_days: float = 14.0
    strategy_prior_kappa: float = 5.0  # Pre-registered; never tune against the sequential evaluation.
    strategy_min_effective_n: float = 5.0

    # Evidence scoring weights
    weight_semantic: float = 0.20
    weight_customer: float = 0.20
    weight_merchant: float = 0.10
    weight_instrument: float = 0.20
    weight_failure_match: float = 0.15
    weight_recency: float = 0.05
    weight_success_outcome: float = 0.10

    @property
    def app_db_abs_path(self) -> Path:
        return Path(self.app_db_path).expanduser().resolve()

    @property
    def waggle_db_abs_path(self) -> Path:
        return Path(self.waggle_db_path).expanduser().resolve()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        # Ensure data directories exist
        _settings.app_db_abs_path.parent.mkdir(parents=True, exist_ok=True)
        _settings.waggle_db_abs_path.parent.mkdir(parents=True, exist_ok=True)
    return _settings


def reset_settings() -> None:
    """Reset cached settings (for testing)."""
    global _settings
    _settings = None
