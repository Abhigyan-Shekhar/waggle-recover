"""SQLite persistence for operational data (separate from Waggle semantic memory)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id TEXT PRIMARY KEY,
    provider_event_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    signature_valid INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(event_type, payment_id)
);

CREATE TABLE IF NOT EXISTS recovery_episodes (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    external_payment_id TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    subscription_id TEXT DEFAULT '',
    mandate_id TEXT DEFAULT '',
    invoice_id TEXT DEFAULT '',
    customer_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(merchant_id, customer_id, scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS payment_failures (
    id TEXT PRIMARY KEY,
    external_payment_id TEXT NOT NULL,
    order_id TEXT DEFAULT '',
    customer_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    method TEXT NOT NULL,
    instrument_id TEXT DEFAULT '',
    route TEXT DEFAULT '',
    failure_code TEXT DEFAULT '',
    failure_reason TEXT DEFAULT '',
    failure_source TEXT DEFAULT '',
    failure_step TEXT DEFAULT '',
    failure_class TEXT DEFAULT 'UNKNOWN',
    occurred_at TEXT NOT NULL,
    raw_event_id TEXT DEFAULT '',
    recovery_episode_id TEXT DEFAULT '',
    waggle_node_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_decisions (
    id TEXT PRIMARY KEY,
    failure_id TEXT NOT NULL,
    action TEXT NOT NULL,
    retry_after_seconds INTEGER DEFAULT NULL,
    recommended_method TEXT DEFAULT NULL,
    recommended_route TEXT DEFAULT NULL,
    confidence REAL DEFAULT 0.5,
    evidence_confidence REAL DEFAULT 0,
    evidence_quality TEXT DEFAULT 'UNKNOWN',
    uncertainty_reason TEXT DEFAULT '',
    abstention_reason TEXT DEFAULT '',
    risk_score INTEGER NOT NULL DEFAULT 0,
    risk_band TEXT DEFAULT 'LOW',
    risk_factors_json TEXT DEFAULT '[]',
    decision_mode TEXT DEFAULT 'deterministic',
    execution_mode TEXT DEFAULT 'simulation',
    decision_latency_ms REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    policy_result TEXT DEFAULT 'ALLOW',
    policy_note TEXT DEFAULT '',
    memory_contribution TEXT DEFAULT 'NONE',
    retrieval_mode TEXT DEFAULT 'FULL_CONTEXT',
    evidence_json TEXT DEFAULT '[]',
    discarded_json TEXT DEFAULT '[]',
    explanation TEXT DEFAULT '',
    human_review_required INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_automated_attempts INTEGER NOT NULL DEFAULT 0,
    last_safe_action TEXT DEFAULT NULL,
    recovery_episode_id TEXT DEFAULT '',
    waggle_node_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (failure_id) REFERENCES payment_failures(id)
);

CREATE TABLE IF NOT EXISTS recovery_attempts (
    id TEXT PRIMARY KEY,
    failure_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    recommended_method TEXT DEFAULT NULL,
    recommended_route TEXT DEFAULT NULL,
    retry_after_seconds INTEGER DEFAULT NULL,
    decision_id TEXT DEFAULT '',
    executed_at TEXT NOT NULL,
    outcome TEXT DEFAULT 'PENDING',
    recovered_amount INTEGER DEFAULT 0,
    failure_reason_if_any TEXT DEFAULT '',
    recovery_episode_id TEXT DEFAULT '',
    waggle_outcome_node_id TEXT DEFAULT NULL,
    FOREIGN KEY (failure_id) REFERENCES payment_failures(id)
);

CREATE TABLE IF NOT EXISTS escalation_records (
    id TEXT PRIMARY KEY,
    recovery_episode_id TEXT NOT NULL,
    failure_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    failure_reason TEXT DEFAULT '',
    attempts_used INTEGER NOT NULL DEFAULT 0,
    max_automated_attempts INTEGER NOT NULL DEFAULT 0,
    candidate_action TEXT NOT NULL,
    policy_result TEXT NOT NULL,
    escalation_reason TEXT NOT NULL,
    accepted_evidence_json TEXT DEFAULT '[]',
    rejected_evidence_json TEXT DEFAULT '[]',
    recommended_manual_next_step TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    waggle_node_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (recovery_episode_id) REFERENCES recovery_episodes(id)
);

CREATE TABLE IF NOT EXISTS payment_instruments (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    fingerprint_or_safe_alias TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    supersedes_instrument_id TEXT DEFAULT NULL,
    last_success_at TEXT DEFAULT NULL,
    waggle_node_id TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    scenario_count INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    started_at TEXT NOT NULL,
    completed_at TEXT DEFAULT NULL,
    results_json TEXT DEFAULT NULL,
    summary_json TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    system TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    action_correct INTEGER NOT NULL DEFAULT 0,
    recovered_amount INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    memory_contribution TEXT DEFAULT 'NONE',
    retrieval_mode TEXT DEFAULT 'FULL_CONTEXT',
    stale_evidence_detected INTEGER DEFAULT 0,
    stale_evidence_correctly_rejected INTEGER DEFAULT 0,
    evidence_count INTEGER DEFAULT 0,
    discarded_count INTEGER DEFAULT 0,
    decision_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES evaluation_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_failures_customer ON payment_failures(customer_id);
CREATE INDEX IF NOT EXISTS idx_failures_merchant ON payment_failures(merchant_id);
CREATE INDEX IF NOT EXISTS idx_decisions_failure ON recovery_decisions(failure_id);
CREATE INDEX IF NOT EXISTS idx_attempts_failure ON recovery_attempts(failure_id);
CREATE INDEX IF NOT EXISTS idx_instruments_customer ON payment_instruments(customer_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON evaluation_results(run_id);
CREATE INDEX IF NOT EXISTS idx_webhook_payment ON webhook_events(payment_id);
CREATE INDEX IF NOT EXISTS idx_escalation_episode ON escalation_records(recovery_episode_id);
"""


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # Existing local demo databases predate the escalation columns.
            # Keep startup migration small and idempotent for clean checkouts
            # as well as already-running hackathon demos.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(recovery_decisions)")}
            additions = {
                "human_review_required": "INTEGER NOT NULL DEFAULT 0",
                "escalation_reason": "TEXT DEFAULT ''",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "max_automated_attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_safe_action": "TEXT DEFAULT NULL",
                "evidence_confidence": "REAL DEFAULT 0",
                "evidence_quality": "TEXT DEFAULT 'UNKNOWN'",
                "uncertainty_reason": "TEXT DEFAULT ''",
                "abstention_reason": "TEXT DEFAULT ''",
                "recovery_episode_id": "TEXT DEFAULT ''",
                "risk_score": "INTEGER NOT NULL DEFAULT 0",
                "risk_band": "TEXT DEFAULT 'LOW'",
                "risk_factors_json": "TEXT DEFAULT '[]'",
                "decision_mode": "TEXT DEFAULT 'deterministic'",
                "execution_mode": "TEXT DEFAULT 'simulation'",
                "decision_latency_ms": "REAL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE recovery_decisions ADD COLUMN {name} {definition}")
            table_additions = {
                "webhook_events": {"provider_event_id": "TEXT NOT NULL DEFAULT ''"},
                "payment_failures": {"recovery_episode_id": "TEXT DEFAULT ''"},
                "recovery_attempts": {"recovery_episode_id": "TEXT DEFAULT ''"},
            }
            for table, definitions in table_additions.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in definitions.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_provider_event "
                "ON webhook_events(provider_event_id) WHERE provider_event_id != ''"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_episode ON payment_failures(recovery_episode_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_attempt_episode ON recovery_attempts(recovery_episode_id)")

    def clear_recovery_data(self) -> None:
        """Clear only this demo application's operational data.

        Waggle memory is cleared separately by the simulator reset endpoint, scoped
        to the Recover tenant. This method intentionally never touches any other DB.
        """
        with self._connect() as conn:
            for table in (
                "evaluation_results", "evaluation_runs", "recovery_attempts",
                "escalation_records", "recovery_decisions", "payment_failures", "recovery_episodes", "payment_instruments",
                "webhook_events",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall()

    def execute_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()

    def execute_write(self, sql: str, params: tuple = ()) -> int:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid or 0

    def upsert_failure(self, failure: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO payment_failures (
                    id, external_payment_id, order_id, customer_id, merchant_id,
                    amount, currency, method, instrument_id, route,
                    failure_code, failure_reason, failure_source, failure_step,
                    failure_class, occurred_at, raw_event_id, waggle_node_id, created_at
                    , recovery_episode_id
                ) VALUES (
                    :id, :external_payment_id, :order_id, :customer_id, :merchant_id,
                    :amount, :currency, :method, :instrument_id, :route,
                    :failure_code, :failure_reason, :failure_source, :failure_step,
                    :failure_class, :occurred_at, :raw_event_id, :waggle_node_id, :created_at
                    , :recovery_episode_id
                )
                ON CONFLICT(id) DO UPDATE SET waggle_node_id = excluded.waggle_node_id
                """,
                failure,
            )
            conn.commit()

    def upsert_decision(self, decision: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recovery_decisions (
                    id, failure_id, action, retry_after_seconds, recommended_method,
                    recommended_route, confidence, reason, status, policy_result,
                    evidence_confidence, evidence_quality, uncertainty_reason, abstention_reason,
                    risk_score, risk_band, risk_factors_json,
                    decision_mode, execution_mode, decision_latency_ms,
                    policy_note, memory_contribution, retrieval_mode, evidence_json,
                    discarded_json, explanation, human_review_required,
                    escalation_reason, attempt_count, max_automated_attempts,
                    last_safe_action, recovery_episode_id, waggle_node_id, created_at
                ) VALUES (
                    :id, :failure_id, :action, :retry_after_seconds, :recommended_method,
                    :recommended_route, :confidence, :reason, :status, :policy_result,
                    :evidence_confidence, :evidence_quality, :uncertainty_reason, :abstention_reason,
                    :risk_score, :risk_band, :risk_factors_json,
                    :decision_mode, :execution_mode, :decision_latency_ms,
                    :policy_note, :memory_contribution, :retrieval_mode, :evidence_json,
                    :discarded_json, :explanation, :human_review_required,
                    :escalation_reason, :attempt_count, :max_automated_attempts,
                    :last_safe_action, :recovery_episode_id, :waggle_node_id, :created_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    waggle_node_id = excluded.waggle_node_id
                """,
                decision,
            )
            conn.commit()

    def upsert_attempt(self, attempt: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recovery_attempts (
                    id, failure_id, customer_id, merchant_id, action_type,
                    recommended_method, recommended_route, retry_after_seconds,
                    decision_id, executed_at, outcome, recovered_amount,
                    failure_reason_if_any, recovery_episode_id, waggle_outcome_node_id
                ) VALUES (
                    :id, :failure_id, :customer_id, :merchant_id, :action_type,
                    :recommended_method, :recommended_route, :retry_after_seconds,
                    :decision_id, :executed_at, :outcome, :recovered_amount,
                    :failure_reason_if_any, :recovery_episode_id, :waggle_outcome_node_id
                )
                ON CONFLICT(id) DO UPDATE SET
                    outcome = excluded.outcome,
                    recovered_amount = excluded.recovered_amount,
                    waggle_outcome_node_id = excluded.waggle_outcome_node_id
                """,
                attempt,
            )
            conn.commit()

    def upsert_instrument(self, instrument: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO payment_instruments (
                    id, customer_id, instrument_type, fingerprint_or_safe_alias,
                    status, created_at, supersedes_instrument_id, last_success_at, waggle_node_id
                ) VALUES (
                    :id, :customer_id, :instrument_type, :fingerprint_or_safe_alias,
                    :status, :created_at, :supersedes_instrument_id, :last_success_at, :waggle_node_id
                )
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    last_success_at = excluded.last_success_at,
                    waggle_node_id = excluded.waggle_node_id
                """,
                instrument,
            )
            conn.commit()

    def upsert_recovery_episode(self, episode: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recovery_episodes (
                    id, scope_type, scope_id, external_payment_id, order_id,
                    subscription_id, mandate_id, invoice_id, customer_id,
                    merchant_id, status, created_at, updated_at
                ) VALUES (
                    :id, :scope_type, :scope_id, :external_payment_id, :order_id,
                    :subscription_id, :mandate_id, :invoice_id, :customer_id,
                    :merchant_id, :status, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    external_payment_id=excluded.external_payment_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                episode,
            )
            conn.commit()

    def get_attempt_count_for_episode(self, recovery_episode_id: str) -> int:
        row = self.execute_one(
            "SELECT COUNT(*) AS cnt FROM recovery_attempts WHERE recovery_episode_id = ?",
            (recovery_episode_id,),
        )
        return int(row["cnt"]) if row else 0

    def get_last_attempt_action_for_episode(self, recovery_episode_id: str) -> str | None:
        row = self.execute_one(
            """
            SELECT action_type FROM recovery_attempts
            WHERE recovery_episode_id = ? AND action_type NOT IN ('STOP', 'ESCALATE')
            ORDER BY executed_at DESC LIMIT 1
            """,
            (recovery_episode_id,),
        )
        return str(row["action_type"]) if row else None

    def upsert_escalation(self, escalation: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO escalation_records (
                    id, recovery_episode_id, failure_id, decision_id, merchant_id,
                    customer_id, amount, failure_reason, attempts_used, max_automated_attempts, candidate_action,
                    policy_result, escalation_reason, accepted_evidence_json,
                    rejected_evidence_json, recommended_manual_next_step, state,
                    waggle_node_id, created_at
                ) VALUES (
                    :id, :recovery_episode_id, :failure_id, :decision_id, :merchant_id,
                    :customer_id, :amount, :failure_reason, :attempts_used, :max_automated_attempts, :candidate_action,
                    :policy_result, :escalation_reason, :accepted_evidence_json,
                    :rejected_evidence_json, :recommended_manual_next_step, :state,
                    :waggle_node_id, :created_at
                )
                ON CONFLICT(id) DO UPDATE SET state=excluded.state, waggle_node_id=excluded.waggle_node_id
                """,
                escalation,
            )
            conn.commit()

    def get_escalations(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.execute(
                "SELECT * FROM escalation_records WHERE state=? ORDER BY created_at DESC",
                (state,),
            )
        else:
            rows = self.execute("SELECT * FROM escalation_records ORDER BY created_at DESC")
        return [dict(row) for row in rows]

    def get_instruments_for_customer(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM payment_instruments WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        return [dict(r) for r in rows]

    def get_failures_for_customer(self, customer_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM payment_failures WHERE customer_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (customer_id, limit),
        )
        return [dict(r) for r in rows]

    def get_decisions_for_failure(self, failure_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM recovery_decisions WHERE failure_id = ? ORDER BY created_at DESC",
            (failure_id,),
        )
        return [dict(r) for r in rows]

    def get_attempts_for_failure(self, failure_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM recovery_attempts WHERE failure_id = ? ORDER BY executed_at DESC",
            (failure_id,),
        )
        return [dict(r) for r in rows]

    def get_attempt_count_for_payment(
        self,
        external_payment_id: str,
        customer_id: str,
        merchant_id: str,
    ) -> int:
        """Count attempts correlated to one payment failure, across webhook retries."""
        rows = self.execute(
            """
            SELECT COUNT(*) as cnt FROM recovery_attempts ra
            JOIN payment_failures pf ON ra.failure_id = pf.id
            WHERE pf.external_payment_id = ?
              AND pf.customer_id = ? AND pf.merchant_id = ?
            """,
            (external_payment_id, customer_id, merchant_id),
        )
        return rows[0]["cnt"] if rows else 0

    def get_last_attempt_action_for_payment(
        self,
        external_payment_id: str,
        customer_id: str,
        merchant_id: str,
    ) -> str | None:
        """Return the last recorded automated action for an audit handoff."""
        row = self.execute_one(
            """
            SELECT ra.action_type
            FROM recovery_attempts ra
            JOIN payment_failures pf ON ra.failure_id = pf.id
            WHERE pf.external_payment_id = ?
              AND pf.customer_id = ? AND pf.merchant_id = ?
              AND ra.action_type NOT IN ('STOP', 'ESCALATE')
            ORDER BY ra.executed_at DESC
            LIMIT 1
            """,
            (external_payment_id, customer_id, merchant_id),
        )
        return str(row["action_type"]) if row else None

    def get_recent_customer_merchant_activity(
        self,
        customer_id: str,
        merchant_id: str,
        window_seconds: int = 3600,
    ) -> int:
        """Return recent cross-payment activity for friction/rate-limit telemetry only."""
        rows = self.execute(
            """
            SELECT COUNT(*) as cnt FROM recovery_attempts ra
            JOIN payment_failures pf ON ra.failure_id = pf.id
            WHERE pf.customer_id = ? AND pf.merchant_id = ?
            AND datetime(ra.executed_at) > datetime('now', ? || ' seconds')
            """,
            (customer_id, merchant_id, f"-{window_seconds}"),
        )
        return rows[0]["cnt"] if rows else 0

    def upsert_webhook_event(self, event: dict[str, Any]) -> bool:
        """Returns True if event is new (not duplicate)."""
        with self._connect() as conn:
            existing = conn.execute("SELECT processed FROM webhook_events WHERE id = ?", (event["id"],)).fetchone()
            if existing is not None:
                # A received-but-failed event is safely retryable; a processed
                # event is a true idempotency duplicate.
                if existing["processed"] == 0:
                    conn.execute(
                        "UPDATE webhook_events SET raw_payload=?, signature_valid=? WHERE id=?",
                        (event["raw_payload"], event["signature_valid"], event["id"]),
                    )
                    conn.commit()
                    return True
                return False
            try:
                conn.execute(
                    """
                    INSERT INTO webhook_events (id, provider_event_id, event_type, payment_id, raw_payload, signature_valid, processed, created_at)
                    VALUES (:id, :provider_event_id, :event_type, :payment_id, :raw_payload, :signature_valid, :processed, :created_at)
                    """,
                    event,
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # Duplicate

    def mark_payment_captured(self, payment_id: str, amount: int) -> int:
        """Close pending attempts correlated through the failed payment ID."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE recovery_attempts SET outcome='SUCCESS', recovered_amount=?
                   WHERE failure_id IN (SELECT id FROM payment_failures WHERE external_payment_id=?)
                   AND outcome IN ('PENDING', 'FAILURE')""",
                (amount, payment_id),
            )
            conn.commit()
            return cur.rowcount

    def get_capture_candidates(self, payment_id: str) -> list[dict[str, Any]]:
        """Return unresolved attempts and graph provenance for a captured payment."""
        rows = self.execute(
            """
            SELECT ra.*, pf.method AS failure_method,
                   pf.instrument_id AS failure_instrument_id,
                   pf.failure_code AS failure_code,
                   pf.waggle_node_id AS failure_waggle_node_id,
                   rd.waggle_node_id AS decision_waggle_node_id
            FROM recovery_attempts ra
            JOIN payment_failures pf ON pf.id = ra.failure_id
            LEFT JOIN recovery_decisions rd ON rd.id = ra.decision_id
            WHERE pf.external_payment_id = ?
              AND ra.outcome IN ('PENDING', 'FAILURE')
            ORDER BY ra.executed_at
            """,
            (payment_id,),
        )
        return [dict(row) for row in rows]

    def mark_attempt_captured(self, attempt_id: str, amount: int, waggle_outcome_node_id: str) -> bool:
        """Persist a confirmed capture only if the attempt is still unresolved."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE recovery_attempts
                SET outcome='SUCCESS', recovered_amount=?, waggle_outcome_node_id=?
                WHERE id=? AND outcome IN ('PENDING', 'FAILURE')
                """,
                (amount, waggle_outcome_node_id, attempt_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def mark_webhook_processed(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE webhook_events SET processed=1 WHERE id=?", (event_id,))
            conn.commit()

    def upsert_evaluation_run(self, run: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO evaluation_runs (id, seed, scenario_count, status, started_at, completed_at, results_json, summary_json)
                VALUES (:id, :seed, :scenario_count, :status, :started_at, :completed_at, :results_json, :summary_json)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    results_json = excluded.results_json,
                    summary_json = excluded.summary_json
                """,
                run,
            )
            conn.commit()

    def insert_evaluation_result(self, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO evaluation_results (
                    id, run_id, scenario_id, system, action_taken, action_correct,
                    recovered_amount, latency_ms, memory_contribution, retrieval_mode,
                    stale_evidence_detected, stale_evidence_correctly_rejected,
                    evidence_count, discarded_count, decision_json, created_at
                ) VALUES (
                    :id, :run_id, :scenario_id, :system, :action_taken, :action_correct,
                    :recovered_amount, :latency_ms, :memory_contribution, :retrieval_mode,
                    :stale_evidence_detected, :stale_evidence_correctly_rejected,
                    :evidence_count, :discarded_count, :decision_json, :created_at
                )
                """,
                result,
            )
            conn.commit()

    def get_all_recoveries(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.execute(
            """
            SELECT pf.*, rd.action, rd.recommended_method, rd.retry_after_seconds,
                   rd.confidence, rd.evidence_confidence, rd.evidence_quality,
                   rd.uncertainty_reason, rd.abstention_reason,
                   rd.risk_score, rd.risk_band, rd.risk_factors_json,
                   rd.reason, rd.status as decision_status,
                   rd.explanation, rd.memory_contribution, rd.evidence_json, rd.discarded_json,
                   rd.policy_result, rd.human_review_required, rd.escalation_reason,
                   rd.attempt_count, rd.max_automated_attempts, rd.last_safe_action,
                   ra.outcome, ra.recovered_amount, ra.executed_at as attempt_at, rd.id as decision_id
            FROM payment_failures pf
            LEFT JOIN recovery_decisions rd ON rd.failure_id = pf.id
            LEFT JOIN recovery_attempts ra ON ra.failure_id = pf.id
            ORDER BY pf.occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_recovery_by_failure_id(self, failure_id: str) -> dict[str, Any] | None:
        row = self.execute_one(
            """
            SELECT pf.*, rd.action, rd.recommended_method, rd.retry_after_seconds,
                   rd.confidence, rd.evidence_confidence, rd.evidence_quality,
                   rd.uncertainty_reason, rd.abstention_reason,
                   rd.risk_score, rd.risk_band, rd.risk_factors_json,
                   rd.reason, rd.status as decision_status,
                   rd.explanation, rd.memory_contribution, rd.evidence_json, rd.discarded_json,
                   rd.policy_result, rd.human_review_required, rd.escalation_reason,
                   rd.attempt_count, rd.max_automated_attempts, rd.last_safe_action,
                   ra.outcome, ra.recovered_amount, rd.id as decision_id
            FROM payment_failures pf
            LEFT JOIN recovery_decisions rd ON rd.failure_id = pf.id
            LEFT JOIN recovery_attempts ra ON ra.failure_id = pf.id
            WHERE pf.id = ?
            """,
            (failure_id,),
        )
        return dict(row) if row else None

    def get_overview_metrics(self) -> dict[str, Any]:
        total_failures = self.execute_one("SELECT COUNT(*) as cnt FROM payment_failures")
        total_amount_at_risk = self.execute_one("SELECT COALESCE(SUM(amount), 0) as total FROM payment_failures")
        recovered = self.execute_one(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(recovered_amount), 0) as total FROM recovery_attempts WHERE outcome = 'SUCCESS'"
        )
        stale_prevented = self.execute_one(
            "SELECT COUNT(*) as cnt FROM recovery_decisions WHERE discarded_json != '[]'"
        )
        policy_violations = self.execute_one(
            "SELECT COUNT(*) as cnt FROM recovery_decisions WHERE policy_result = 'BLOCK'"
        )
        episodes = self.execute_one("SELECT COUNT(*) as cnt FROM recovery_episodes")
        escalations = self.execute_one("SELECT COUNT(*) as cnt FROM escalation_records")
        stopped = self.execute_one(
            "SELECT COUNT(*) as cnt FROM recovery_decisions WHERE action IN ('STOP', 'ESCALATE')"
        )
        automatic = self.execute_one(
            "SELECT COUNT(*) as cnt FROM recovery_attempts WHERE action_type NOT IN ('STOP', 'ESCALATE')"
        )
        qwen_modified = self.execute_one(
            "SELECT COUNT(*) as cnt FROM recovery_decisions WHERE decision_mode = 'agent' AND policy_result = 'MODIFY'"
        )
        qwen_blocked = self.execute_one(
            "SELECT COUNT(*) as cnt FROM recovery_decisions WHERE decision_mode = 'agent' AND policy_result = 'BLOCK'"
        )
        latency = self.execute_one(
            "SELECT COALESCE(AVG(decision_latency_ms), 0) as avg FROM recovery_decisions"
        )

        total_fail = total_failures["cnt"] if total_failures else 0
        amount_at_risk = total_amount_at_risk["total"] if total_amount_at_risk else 0
        rec_count = recovered["cnt"] if recovered else 0
        rec_amount = recovered["total"] if recovered else 0
        stale = stale_prevented["cnt"] if stale_prevented else 0
        violations = policy_violations["cnt"] if policy_violations else 0

        rate = (rec_count / total_fail * 100) if total_fail > 0 else 0.0

        return {
            "total_failures": total_fail,
            "gmv_at_risk": amount_at_risk,
            "recovered_gmv": rec_amount,
            "recovery_count": rec_count,
            "recovery_rate_pct": round(rate, 1),
            "stale_evidence_prevented": stale,
            "policy_violations": violations,
            "operational": {
                "label": "Operational prototype metrics",
                "mode_breakdown": [
                    dict(row) for row in self.execute(
                        "SELECT execution_mode as mode, COUNT(*) as total_risk_events "
                        "FROM recovery_decisions GROUP BY execution_mode ORDER BY execution_mode"
                    )
                ],
                "total_risk_events": total_fail,
                "total_recovery_episodes": episodes["cnt"] if episodes else 0,
                "automatic_recoveries": automatic["cnt"] if automatic else 0,
                "escalations": escalations["cnt"] if escalations else 0,
                "stopped_recoveries": stopped["cnt"] if stopped else 0,
                "simulated_recovered_gmv": rec_amount,
                "stale_memories_rejected": stale,
                "qwen_proposals_modified_by_policy": qwen_modified["cnt"] if qwen_modified else 0,
                "qwen_proposals_blocked_by_policy": qwen_blocked["cnt"] if qwen_blocked else 0,
                "average_decision_latency_ms": round(float(latency["avg"] if latency else 0), 2),
            },
        }
