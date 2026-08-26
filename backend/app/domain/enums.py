"""Domain enums for Waggle Recover — isolated from Waggle Core."""
from __future__ import annotations

from enum import StrEnum


class RecoveryAction(StrEnum):
    RETRY_NOW = "RETRY_NOW"
    RETRY_AFTER = "RETRY_AFTER"
    SUGGEST_METHOD = "SUGGEST_METHOD"
    CUSTOMER_NUDGE = "CUSTOMER_NUDGE"
    WAIT_NEXT_CYCLE = "WAIT_NEXT_CYCLE"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


class TemporalStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class MemoryContribution(StrEnum):
    NONE = "NONE"
    LOOKUP_FIRST = "LOOKUP_FIRST"
    FULL_CONTEXT = "FULL_CONTEXT"
    PARTIAL = "PARTIAL"


class OutcomeStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"


class RetrievalMode(StrEnum):
    LOOKUP_FIRST = "LOOKUP_FIRST"
    FULL_CONTEXT = "FULL_CONTEXT"


class PolicyResult(StrEnum):
    ALLOW = "ALLOW"
    MODIFY = "MODIFY"
    BLOCK = "BLOCK"


class FailureClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    INSTRUMENT = "INSTRUMENT"
    BALANCE = "BALANCE"
    ROUTE = "ROUTE"
    UNKNOWN = "UNKNOWN"


# Failure codes → classification mapping
TRANSIENT_CODES = {
    "issuer_unavailable",
    "network_error",
    "gateway_timeout",
    "upstream_timeout",
    "server_error",
    "payment_declined_temporarily",
    "route_degraded",
}

PERMANENT_CODES = {
    "expired_card",
    "invalid_card",
    "lost_stolen_card",
    "card_blocked",
    "do_not_honour",
    "invalid_account",
    "account_closed",
    "incorrect_credentials",
}

BALANCE_CODES = {
    "insufficient_funds",
    "low_balance",
    "credit_limit_exceeded",
}

INSTRUMENT_CODES = {
    "invalid_instrument",
    "instrument_unavailable",
    "expired_instrument",
}


def classify_failure(failure_code: str, failure_reason: str = "") -> FailureClass:
    code = (failure_code or "").lower().strip()
    reason = (failure_reason or "").lower().strip()

    if code in TRANSIENT_CODES or any(t in reason for t in ["temporary", "timeout", "unavailable", "degraded"]):
        return FailureClass.TRANSIENT
    if code in PERMANENT_CODES or any(t in reason for t in ["expired", "invalid", "blocked", "stolen"]):
        return FailureClass.PERMANENT
    if code in BALANCE_CODES or any(t in reason for t in ["insufficient", "balance", "limit"]):
        return FailureClass.BALANCE
    if code in INSTRUMENT_CODES:
        return FailureClass.INSTRUMENT
    return FailureClass.UNKNOWN
