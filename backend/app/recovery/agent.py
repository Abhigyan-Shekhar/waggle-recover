"""Constrained LangGraph recovery agent backed by Groq-hosted Qwen models.

The agent only proposes a candidate action. Waggle decides which memories are
authoritative before this graph runs, and PolicyEngine remains the final safety
authority after it completes.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, TypedDict

from app.domain.enums import RecoveryAction, TemporalStatus
from app.domain.models import EvidenceBundle, EvidenceReference, RecoveryDecision
from app.recovery.decision_engine import DecisionProvider, DeterministicDecisionProvider

LOGGER = logging.getLogger(__name__)

SUPPORTED_PAYMENT_METHODS = {"card", "upi", "netbanking", "wallet", "emi", "paylater"}
PREFERRED_PAYMENT_METHODS = ("upi", "netbanking", "wallet", "card", "emi", "paylater")
TECHNICAL_MIN_RETRY_SECONDS = 60
TECHNICAL_MAX_RETRY_SECONDS = 7200
AGENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [action.value for action in RecoveryAction]},
        "retry_after_seconds": {
            "anyOf": [
                {
                    "type": "integer",
                    "minimum": TECHNICAL_MIN_RETRY_SECONDS,
                    "maximum": TECHNICAL_MAX_RETRY_SECONDS,
                },
                {"type": "null"},
            ],
        },
        "recommended_method": {
            "anyOf": [
                {"type": "string", "enum": list(PREFERRED_PAYMENT_METHODS)},
                {"type": "null"},
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
    },
    "required": [
        "action",
        "retry_after_seconds",
        "recommended_method",
        "confidence",
        "reason",
        "evidence_ids",
    ],
    "additionalProperties": False,
}


class AgentModelClient(Protocol):
    """Small injectable boundary so tests never need a network call."""

    def complete(
        self,
        *,
        system_prompt: str,
        trusted_context: dict[str, Any],
        model: str,
        temperature: float,
    ) -> str: ...


class GroqQwenClient:
    """Official Groq SDK adapter with explicit credentials and timeout."""

    def __init__(self, api_key: str, timeout_seconds: float) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def complete(
        self,
        *,
        system_prompt: str,
        trusted_context: dict[str, Any],
        model: str,
        temperature: float,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        if not model:
            raise RuntimeError("GROQ_MODEL is not configured")

        from groq import Groq

        client = Groq(api_key=self.api_key, timeout=self.timeout_seconds)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(trusted_context, separators=(",", ":"))},
            ],
            temperature=temperature,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "recovery_candidate",
                    "strict": True,
                    "schema": AGENT_RESPONSE_SCHEMA,
                },
            },
        )
        return response.choices[0].message.content or ""


class AgentState(TypedDict, total=False):
    """Minimal auditable state passed through the explicit LangGraph."""

    bundle: EvidenceBundle
    trusted_context: dict[str, Any]
    model_response: str
    parsed_candidate: dict[str, Any]
    candidate_decision: RecoveryDecision
    validation_errors: list[str]
    agent_trace: dict[str, Any]


class AgentDecisionProvider(DecisionProvider):
    """LangGraph provider that reasons only after Waggle temporal validation."""

    mode = "agent"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        temperature: float = 0.0,
        timeout_seconds: float = 15.0,
        model_client: AgentModelClient | None = None,
        fallback: DecisionProvider | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.model_client = model_client or GroqQwenClient(api_key=api_key, timeout_seconds=timeout_seconds)
        self.fallback = fallback or DeterministicDecisionProvider()
        self._graph = None
        self._graph_error = ""
        try:
            self._graph = self._build_graph()
        except Exception as exc:  # Optional dependency/configuration must fail safely.
            self._graph_error = f"LangGraph unavailable ({type(exc).__name__})"
            LOGGER.warning(self._graph_error)

    def _build_graph(self):
        from langgraph.graph import END, START, StateGraph

        workflow = StateGraph(AgentState)
        workflow.add_node("prepare_trusted_context", self._prepare_trusted_context)
        workflow.add_node("reason_with_model", self._reason_with_model)
        workflow.add_node("validate_model_output", self._validate_model_output)
        workflow.add_node("safe_fallback", self._safe_fallback)
        workflow.add_edge(START, "prepare_trusted_context")
        workflow.add_edge("prepare_trusted_context", "reason_with_model")
        workflow.add_edge("reason_with_model", "validate_model_output")
        workflow.add_conditional_edges(
            "validate_model_output",
            lambda state: "valid" if state.get("candidate_decision") is not None else "invalid",
            {"valid": END, "invalid": "safe_fallback"},
        )
        workflow.add_edge("safe_fallback", END)
        return workflow.compile()

    def decide(self, bundle: EvidenceBundle) -> RecoveryDecision:
        decision, _ = self.decide_with_trace(bundle)
        return decision

    def decide_with_trace(self, bundle: EvidenceBundle) -> tuple[RecoveryDecision, dict[str, Any]]:
        if self._graph is None:
            state = self._safe_fallback({
                "bundle": bundle,
                "validation_errors": [self._graph_error or "LangGraph initialization failed"],
                "agent_trace": self._initial_trace(bundle),
            })
        else:
            try:
                state = self._graph.invoke({"bundle": bundle, "agent_trace": self._initial_trace(bundle)})
            except Exception as exc:
                state = self._safe_fallback({
                    "bundle": bundle,
                    "validation_errors": [f"Agent graph failed ({type(exc).__name__})"],
                    "agent_trace": self._initial_trace(bundle),
                })

        decision = state["candidate_decision"]
        trace = state["agent_trace"]
        return decision, trace

    def _prepare_trusted_context(self, state: AgentState) -> dict[str, Any]:
        bundle = state["bundle"]
        context = self.build_trusted_context(bundle)
        trace = dict(state["agent_trace"])
        trace["stages"] = [
            {
                "key": "semantic_memory",
                "label": "Semantic Memory Retrieval",
                "status": "complete",
                "detail": f"Waggle retrieved {len(bundle.accepted_evidence) + len(bundle.discarded_evidence)} relevant memories.",
            },
            {
                "key": "temporal_validation",
                "label": "Temporal Validation",
                "status": "warning" if bundle.discarded_evidence else "complete",
                "detail": (
                    f"{len(bundle.accepted_evidence)} trusted; {len(bundle.discarded_evidence)} rejected as stale/superseded."
                ),
            },
        ]
        return {"trusted_context": context, "agent_trace": trace}

    def _reason_with_model(self, state: AgentState) -> dict[str, Any]:
        trace = dict(state["agent_trace"])
        started = time.perf_counter()
        try:
            response = self.model_client.complete(
                system_prompt=self.system_prompt(),
                trusted_context=state["trusted_context"],
                model=self.model,
                temperature=self.temperature,
            )
            errors: list[str] = []
            status = "complete"
            detail = "Qwen returned a structured candidate for deterministic validation."
        except TimeoutError:
            response = ""
            errors = ["Model call timed out"]
            status = "fallback"
            detail = "Qwen timed out; safe deterministic fallback selected."
        except Exception as exc:
            response = ""
            errors = [f"Model call failed ({type(exc).__name__})"]
            status = "fallback"
            detail = "Qwen was unavailable; safe deterministic fallback selected."

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        trace["model_latency_ms"] = latency_ms
        trace["stages"] = [
            *trace.get("stages", []),
            {"key": "agent_reasoning", "label": "Qwen Agent Reasoning", "status": status, "detail": detail},
        ]
        return {"model_response": response, "validation_errors": errors, "agent_trace": trace}

    def _validate_model_output(self, state: AgentState) -> dict[str, Any]:
        errors = list(state.get("validation_errors", []))
        candidate_data: dict[str, Any] = {}
        if not errors:
            try:
                parsed = json.loads(state.get("model_response", ""))
                if not isinstance(parsed, dict):
                    raise ValueError("top-level output is not an object")
                candidate_data = parsed
            except Exception:
                errors.append("Model returned malformed JSON")

        decision = None
        if not errors:
            decision, errors = self._candidate_from_data(candidate_data, state["bundle"])

        trace = dict(state["agent_trace"])
        trace["validation_errors"] = errors
        if decision is not None:
            trace.update({
                "candidate_action": decision.action.value,
                "candidate_retry_after_seconds": decision.retry_after_seconds,
                "candidate_recommended_method": decision.recommended_method,
                "candidate_reason": decision.reason,
                "cited_evidence_ids": [ref.waggle_node_id for ref in decision.evidence_references],
                "agent_fallback": False,
                "fallback_reason": None,
            })
        return {
            "parsed_candidate": candidate_data,
            "candidate_decision": decision,
            "validation_errors": errors,
            "agent_trace": trace,
        }

    def _safe_fallback(self, state: AgentState) -> dict[str, Any]:
        bundle = state["bundle"]
        decision = self.fallback.decide(bundle)
        errors = list(state.get("validation_errors", [])) or ["Agent produced no valid candidate"]
        trace = dict(state.get("agent_trace") or self._initial_trace(bundle))
        if not trace.get("stages"):
            trace["stages"] = [
                {"key": "semantic_memory", "label": "Semantic Memory Retrieval", "status": "complete", "detail": "Waggle context retained."},
                {"key": "temporal_validation", "label": "Temporal Validation", "status": "complete", "detail": "Trusted evidence boundary retained."},
            ]
        if not any(stage.get("key") == "agent_reasoning" for stage in trace["stages"]):
            trace["stages"].append({
                "key": "agent_reasoning",
                "label": "Qwen Agent Reasoning",
                "status": "fallback",
                "detail": "Agent unavailable; safe deterministic fallback selected.",
            })
        trace.update({
            "candidate_action": decision.action.value,
            "candidate_retry_after_seconds": decision.retry_after_seconds,
            "candidate_recommended_method": decision.recommended_method,
            "candidate_reason": decision.reason,
            "cited_evidence_ids": [ref.waggle_node_id for ref in decision.evidence_references],
            "validation_errors": errors,
            "agent_fallback": True,
            "fallback_reason": "; ".join(errors),
        })
        return {"candidate_decision": decision, "validation_errors": errors, "agent_trace": trace}

    def _candidate_from_data(
        self,
        data: dict[str, Any],
        bundle: EvidenceBundle,
    ) -> tuple[RecoveryDecision | None, list[str]]:
        errors: list[str] = []
        raw_action = str(data.get("action", "")).upper().strip()
        try:
            action = RecoveryAction(raw_action)
        except ValueError:
            action = RecoveryAction.STOP
            errors.append("Action is not in RecoveryAction")

        retry_seconds = data.get("retry_after_seconds")
        if retry_seconds is not None and (isinstance(retry_seconds, bool) or not isinstance(retry_seconds, int)):
            errors.append("retry_after_seconds must be an integer or null")
        if isinstance(retry_seconds, int) and not TECHNICAL_MIN_RETRY_SECONDS <= retry_seconds <= TECHNICAL_MAX_RETRY_SECONDS:
            errors.append("retry_after_seconds is outside technical bounds")
        if action == RecoveryAction.RETRY_AFTER and retry_seconds is None:
            errors.append("RETRY_AFTER requires retry_after_seconds")

        method = data.get("recommended_method")
        if method is not None:
            method = str(method).lower().strip()
            if method not in SUPPORTED_PAYMENT_METHODS:
                errors.append("recommended_method is not a supported payment method")
        if action == RecoveryAction.SUGGEST_METHOD and not method:
            errors.append("SUGGEST_METHOD requires recommended_method")

        # Normalize fields that are not meaningful for the selected action so
        # the candidate and audit trace cannot contain contradictory parameters.
        if action != RecoveryAction.RETRY_AFTER:
            retry_seconds = None
        if action not in (RecoveryAction.RETRY_AFTER, RecoveryAction.SUGGEST_METHOD):
            method = None

        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            errors.append("confidence must be between 0 and 1")

        reason = str(data.get("reason", "")).strip()
        if not reason:
            errors.append("reason is required")

        evidence_ids = data.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or not all(isinstance(item, str) for item in evidence_ids):
            evidence_ids = []
            errors.append("evidence_ids must be a string array")

        accepted_by_id = {ref.waggle_node_id: ref for ref in bundle.accepted_evidence}
        rejected_ids = {ref.waggle_node_id for ref in bundle.discarded_evidence}
        cited = set(evidence_ids)
        if cited & rejected_ids:
            errors.append("Model cited rejected evidence")
        if cited - accepted_by_id.keys() - rejected_ids:
            errors.append("Model cited unknown evidence")
        cited_refs = [accepted_by_id[node_id] for node_id in evidence_ids if node_id in accepted_by_id]
        if any(ref.temporal_status in (TemporalStatus.STALE, TemporalStatus.SUPERSEDED) for ref in cited_refs):
            errors.append("Model cited stale or superseded evidence")

        if errors:
            return None, errors
        return RecoveryDecision(
            failure_id=bundle.current_failure.id,
            action=action,
            retry_after_seconds=retry_seconds,
            recommended_method=method,
            confidence=float(confidence),
            reason=reason[:500],
            memory_contribution=bundle.memory_contribution,
            evidence_references=cited_refs,
            discarded_evidence=bundle.discarded_evidence,
        ), []

    @staticmethod
    def build_trusted_context(bundle: EvidenceBundle) -> dict[str, Any]:
        failure = bundle.current_failure
        policy = bundle.merchant_policy
        blocked_methods = set(policy.blocked_methods if policy is not None else [])
        safe_alternatives = [
            method
            for method in PREFERRED_PAYMENT_METHODS
            if method != failure.method and method not in blocked_methods
        ]
        return {
            "instruction": "Produce a candidate recovery action only. Never execute money movement.",
            "current_failure": {
                "payment_id": failure.external_payment_id,
                "customer_id": failure.customer_id,
                "merchant_id": failure.merchant_id,
                "amount": failure.amount,
                "currency": failure.currency,
                "method": failure.method,
                "instrument_id": failure.instrument_id,
                "failure_code": failure.failure_code,
                "failure_class": failure.failure_class.value,
                "retry_count": bundle.retry_count,
            },
            "current_instruments": [
                {
                    "alias": item.fingerprint_or_safe_alias,
                    "type": item.instrument_type,
                    "status": item.status,
                }
                for item in bundle.current_instruments
            ],
            "safe_alternative_methods": safe_alternatives,
            "trusted_historical_evidence": [AgentDecisionProvider._evidence_summary(ref, usable=True) for ref in bundle.accepted_evidence],
            "rejected_memory_for_transparency_only": [
                AgentDecisionProvider._evidence_summary(ref, usable=False) for ref in bundle.discarded_evidence
            ],
            "merchant_policy": None if policy is None else {
                "allowed_actions": [action.value for action in policy.allowed_actions],
                "max_recovery_attempts": policy.max_recovery_attempts,
                "min_retry_interval_seconds": policy.min_retry_interval_seconds,
                "max_retry_interval_seconds": policy.max_retry_interval_seconds,
                "blocked_methods": policy.blocked_methods,
                "blocked_routes": policy.blocked_routes,
            },
            "retrieval": {
                "mode": bundle.retrieval_mode.value,
                "memory_contribution": bundle.memory_contribution.value,
            },
        }

    @staticmethod
    def _evidence_summary(ref: EvidenceReference, *, usable: bool) -> dict[str, Any]:
        metadata = ref.metadata or {}
        return {
            "evidence_id": ref.waggle_node_id,
            "type": ref.memory_type,
            "label": ref.label,
            "usable_as_evidence": usable,
            "action": metadata.get("action_type") or metadata.get("action"),
            "outcome": metadata.get("outcome"),
            "method": metadata.get("method") or metadata.get("recommended_method"),
            "instrument_id": metadata.get("instrument_id") or metadata.get("alias"),
            "retry_after_seconds": metadata.get("retry_after_seconds"),
            "temporal_status": ref.temporal_status.value,
            "relevance_score": round(ref.relevance_score, 4),
            "rejection_reason": None if usable else ref.rejection_reason,
        }

    @staticmethod
    def system_prompt() -> str:
        return (
            "You are a constrained payment recovery decision agent. Waggle has already decided which memories are "
            "authoritative. Use ONLY trusted_historical_evidence as evidence. Items under "
            "rejected_memory_for_transparency_only are forbidden evidence: never cite them, reuse their timing, or "
            "override their stale/superseded status. You produce a candidate action only; you never execute payments. "
            "When supersession rejects the retrieved success/timing memory and no trusted timing evidence remains, "
            "prefer SUGGEST_METHOD using safe_alternative_methods over retrying the same failed method. "
            "Set retry_after_seconds to null unless action is RETRY_AFTER, and set recommended_method to null unless "
            "the selected action uses a payment method. "
            "Return one JSON object with exactly these fields: action, retry_after_seconds, recommended_method, "
            "confidence, reason, evidence_ids. action must be an existing RecoveryAction. evidence_ids may contain "
            "only accepted evidence IDs. Give a short auditable reason, not hidden chain-of-thought."
        )

    def _initial_trace(self, bundle: EvidenceBundle) -> dict[str, Any]:
        return {
            "decision_mode": "agent",
            "model_provider": "groq",
            "model": self.model or "not configured",
            "retrieval_mode": bundle.retrieval_mode.value,
            "memory_contribution": bundle.memory_contribution.value,
            "accepted_evidence_ids": [ref.waggle_node_id for ref in bundle.accepted_evidence],
            "rejected_evidence_ids": [ref.waggle_node_id for ref in bundle.discarded_evidence],
            "agent_fallback": False,
            "model_latency_ms": 0.0,
            "stages": [],
        }
