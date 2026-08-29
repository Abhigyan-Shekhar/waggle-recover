import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

type Metrics = { gmv_at_risk: number; recovered_gmv: number; recovery_rate_pct: number; stale_evidence_prevented: number; policy_violations: number };
type Evidence = { rejection_reason?: string; metadata?: { instrument_id?: string; retry_after_seconds?: number } };
type Recovery = { id: string; customer_id: string; amount: number; method: string; instrument_id?: string; failure_code: string; action?: string; recommended_method?: string; retry_after_seconds?: number; outcome?: string; recovered_amount?: number; explanation?: string; discarded_json?: Evidence[]; policy_result?: string; human_review_required?: boolean | number; escalation_reason?: string; attempt_count?: number; max_automated_attempts?: number; last_safe_action?: string };
type Scenario = { id: string; name: string; category?: string; has_stale_memory?: boolean; has_useful_memory?: boolean };
type SystemMetrics = { name: string; action_accuracy_pct: number; success_rate_pct: number; recovery_rate_gmv_pct: number; stale_rejection_rate_pct: number; avg_latency_ms: number };
type EvaluationSummary = { scenario_count: number; systems: Record<"baseline_a" | "baseline_b" | "system_c", SystemMetrics> };
type GraphNode = { id: string; label: string; node_type: string; tags?: string[]; metadata?: Record<string, unknown>; valid_to?: string | null };
type GraphEdge = { source_id: string; target_id: string; relationship: string; metadata?: Record<string, unknown> };
type MemoryGraph = { root_id?: string; nodes: GraphNode[]; edges: GraphEdge[]; message?: string };
type DecisionMode = "deterministic" | "agent";
type AgentStage = { key: string; label: string; status: "complete" | "warning" | "fallback"; detail: string };
type AgentTrace = {
  decision_mode: "agent"; model_provider: string; model: string; candidate_action?: string;
  candidate_retry_after_seconds?: number | null; candidate_recommended_method?: string | null;
  candidate_reason?: string; policy_result?: string; final_action?: string;
  final_retry_after_seconds?: number | null; final_recommended_method?: string | null;
  agent_fallback: boolean; fallback_reason?: string | null; model_latency_ms: number; stages: AgentStage[];
};
type StrategyPrior = {
  action: string; recommended_method?: string | null; posterior_success_probability: number;
  global_prior: number; effective_n: number; insufficient_history: boolean; selected_bucket: string;
  authoritative_evidence_ids: string[];
};

const money = (paise = 0) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(paise / 100);
const actionSummary = (action?: string | null, retry?: number | null, method?: string | null) =>
  `${action?.replaceAll("_", " ") ?? "—"}${retry != null ? ` · ${retry}s` : method ? ` · ${method.toUpperCase()}` : ""}`;
const scenarioCopy: Record<string, { index: string; description: string }> = {
  curated_001: { index: "01", description: "Avoid a blind retry when a proven alternative method exists." },
  curated_002: { index: "02", description: "Reuse an exact, validated retry interval for the same failure." },
  curated_003: { index: "03", description: "Reject a successful retry tied to a card that was replaced." },
  curated_004: { index: "04", description: "Retrieve merchant-specific outcomes without weakening policy." },
  curated_005: { index: "05", description: "Show the safe fallback when no useful history exists." },
  curated_006: { index: "06", description: "Stop automated recovery and hand the case to a human when policy blocks it." },
};

function BrandMark() {
  return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>;
}

function GraphView({ graph, loading }: { graph: MemoryGraph | null; loading: boolean }) {
  if (loading) return <div className="graph-empty graph-loading" role="status"><span className="spinner" /><strong>Tracing memory relationships…</strong><span>Following failure, evidence, decision, and outcome edges.</span></div>;
  if (!graph?.nodes.length) return <div className="graph-empty"><BrandMark /><strong>No decision graph selected</strong><span>Run a scenario or open a recovery to reveal its temporal memory trail.</span></div>;

  const uniqueNodes = Array.from(new Map(graph.nodes.map(node => [node.id, node])).values());
  const rootId = graph.root_id ?? uniqueNodes.find(node => node.tags?.includes("recovery_decision"))?.id;
  const escalationGraph = Boolean(uniqueNodes.find(node => node.id === rootId)?.metadata?.human_review_required);
  const relation = (name: string) => graph.edges.find(edge => edge.metadata?.relation === name);
  const currentFailureId = relation("decision_for_failure")?.target_id;
  const currentOutcomeId = relation("outcome_of_decision")?.source_id;
  const rejectedIds = new Set(graph.edges.filter(edge => edge.metadata?.validation_status === "rejected").map(edge => edge.target_id));
  const acceptedIds = new Set(graph.edges.filter(edge => edge.metadata?.validation_status === "accepted").map(edge => edge.target_id));
  const currentInstrument = String(uniqueNodes.find(node => node.id === currentFailureId)?.metadata?.instrument_id ?? "");
  const roleFor = (node: GraphNode) => {
    if (node.id === rootId) return "decision";
    if (node.id === currentFailureId) return "failure";
    if (node.id === currentOutcomeId) return "outcome";
    if (rejectedIds.has(node.id)) return "rejected";
    if (acceptedIds.has(node.id)) return "accepted";
    if (node.tags?.includes("payment_instrument")) {
      const superseded = Boolean(node.valid_to || node.metadata?.superseded_by || node.tags.some(tag => tag === "status:superseded"));
      return superseded ? "superseded" : String(node.metadata?.alias ?? "") === currentInstrument ? "current-instrument" : "memory";
    }
    return "memory";
  };
  const rejectedNodes = uniqueNodes.filter(node => roleFor(node) === "rejected").sort((a, b) => Number(b.metadata?.retry_after_seconds === 480) - Number(a.metadata?.retry_after_seconds === 480)).slice(0, 1);
  const updateEdge = graph.edges.find(edge => String(edge.metadata?.relation ?? edge.relationship) === "updates");
  const supersededInstrument = uniqueNodes.find(node => node.id === updateEdge?.target_id) ?? uniqueNodes.find(node => roleFor(node) === "superseded");
  const activeInstrument = uniqueNodes.find(node => node.id === updateEdge?.source_id) ?? uniqueNodes.find(node => roleFor(node) === "current-instrument");
  const acceptedEvidence = uniqueNodes.find(node => roleFor(node) === "accepted");
  const essentialIds = new Set([rootId, currentFailureId, currentOutcomeId, supersededInstrument?.id, activeInstrument?.id, acceptedEvidence?.id, ...rejectedNodes.map(node => node.id)].filter(Boolean));
  const nodes = uniqueNodes.filter(node => essentialIds.has(node.id)).slice(0, 7);
  const roleCounts: Record<string, number> = {};
  const slots: Record<string, Array<{ x: number; y: number }>> = {
    failure: [{ x: 112, y: 108 }], rejected: [{ x: 112, y: 298 }], superseded: [{ x: 330, y: 354 }],
    "current-instrument": [{ x: 540, y: 354 }], decision: [{ x: 424, y: 128 }], outcome: [{ x: 704, y: 128 }],
    accepted: [{ x: 235, y: 245 }], memory: [{ x: 676, y: 305 }],
  };
  const positions = new Map(nodes.map(node => {
    const role = roleFor(node);
    const index = roleCounts[role] ?? 0;
    roleCounts[role] = index + 1;
    return [node.id, slots[role]?.[index] ?? { x: 675, y: 300 + index * 75 }] as const;
  }));
  const storyRelations = new Set(["updates", "decision_for_failure", "outcome_of_decision", "rejected_evidence", "accepted_evidence"]);
  const edges = graph.edges.filter(edge => positions.has(edge.source_id) && positions.has(edge.target_id) && storyRelations.has(String(edge.metadata?.relation ?? edge.relationship)));
  const titleFor = (node: GraphNode) => {
    const role = roleFor(node);
    if (role === "failure") return "Current failure";
    if (role === "decision" && node.metadata?.human_review_required) return "ESCALATE";
    if (role === "decision") return Number(node.metadata?.retry_after_seconds ?? 0) ? `Retry ${node.metadata?.retry_after_seconds}s` : `Suggest ${String(node.metadata?.recommended_method ?? "method").toUpperCase()}`;
    if (role === "outcome" && String(node.metadata?.action_type) === "ESCALATE") return "Human review";
    if (role === "outcome") return `${String(node.metadata?.outcome ?? "Recovery")} outcome`;
    if (role === "accepted" && escalationGraph) return "Prior attempts";
    if (role === "rejected") return "Old success memory";
    if (["superseded", "current-instrument"].includes(role)) return String(node.metadata?.alias ?? node.label);
    return node.label.replace(/^Recovery /, "").slice(0, 24);
  };
  const detailFor = (node: GraphNode) => {
    const role = roleFor(node);
    if (role === "failure") return `${String(node.metadata?.instrument_id ?? "")} · ${String(node.metadata?.failure_code ?? "")}`;
    if (role === "decision") return node.metadata?.human_review_required ? "POLICY BLOCK → HANDOFF" : "POLICY-VALIDATED ACTION";
    if (role === "outcome") return String(node.metadata?.action_type) === "ESCALATE" ? "NO MONEY MOVEMENT" : Number(node.metadata?.recovered_amount) ? `${money(Number(node.metadata?.recovered_amount))} recovered` : "RESULT CAPTURED";
    if (role === "accepted" && escalationGraph) return "RECORDED RECOVERY HISTORY";
    if (role === "rejected") return `${String(node.metadata?.instrument_id ?? "old card")} · ${Math.round(Number(node.metadata?.retry_after_seconds ?? 0) / 60)} min retry`;
    if (role === "superseded") return "SUPERSEDED CARD";
    if (role === "current-instrument") return "CURRENT CARD";
    return node.node_type.toUpperCase();
  };
  const edgeLabel = (edge: GraphEdge) => ({
    decision_for_failure: "DECIDES", outcome_of_decision: "PRODUCED", rejected_evidence: "REJECTED AS STALE",
    accepted_evidence: "USED AS MEMORY", updates: "SUPERSEDES",
  } as Record<string, string>)[String(edge.metadata?.relation ?? edge.relationship)] ?? edge.relationship.replaceAll("_", " ").toUpperCase();

  return <div className="graph-wrap">
    <div className="graph-stats"><span><b>{uniqueNodes.length}</b> nodes in audit</span><span className="rejected-stat"><b>{rejectedIds.size}</b> stale rejected</span><span><b>1</b> final decision</span></div>
    <svg viewBox="0 0 820 440" role="img" aria-label="Waggle graph connecting payment failure, validated or rejected memory, final recovery decision, and outcome">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>
      {edges.map((edge, index) => {
        const semantic = String(edge.metadata?.relation ?? edge.relationship);
        const reverse = ["decision_for_failure", "outcome_of_decision", "rejected_evidence", "updates"].includes(semantic);
        const source = positions.get(reverse ? edge.target_id : edge.source_id)!;
        const target = positions.get(reverse ? edge.source_id : edge.target_id)!;
        const labelY = semantic === "updates" ? source.y - 52 : (source.y + target.y) / 2 + (semantic === "decision_for_failure" ? 12 : semantic === "rejected_evidence" ? -14 : -8);
        return <g key={`${edge.source_id}-${edge.target_id}-${index}`}>
          <line className={`edge ${edge.metadata?.validation_status === "rejected" ? "rejected-edge" : ""}`} x1={source.x} y1={source.y} x2={target.x} y2={target.y} markerEnd="url(#arrow)" />
          <text className="edge-label" textAnchor="middle" x={(source.x + target.x) / 2} y={labelY}>{edgeLabel(edge)}</text>
        </g>;
      })}
      {nodes.map(node => {
        const position = positions.get(node.id)!;
        const role = roleFor(node);
        return <g className={`graph-node ${role}`} key={node.id} transform={`translate(${position.x} ${position.y})`}>
          <rect x="-82" y="-39" width="164" height="78" rx="8" />
          <text className="node-label" textAnchor="middle" y="-7">{titleFor(node).slice(0, 25)}</text>
          <text className="node-type" textAnchor="middle" y="14">{detailFor(node).slice(0, 31)}</text>
        </g>;
      })}
    </svg>
    <div className="graph-legend"><span><i className="memory-dot" /> Stored event</span><span><i className="stale-dot" /> Stale / superseded</span><span><i className="decision-dot" /> Decision / outcome</span></div>
  </div>;
}

function App() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [recoveries, setRecoveries] = useState<Recovery[]>([]);
  const [selected, setSelected] = useState<Recovery | null>(null);
  const [graph, setGraph] = useState<MemoryGraph | null>(null);
  const [evaluation, setEvaluation] = useState<EvaluationSummary | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [message, setMessage] = useState("Ready. Choose a scenario to inspect how memory changes the decision.");
  const [decisionMode, setDecisionMode] = useState<DecisionMode>("deterministic");
  const [agentTrace, setAgentTrace] = useState<AgentTrace | null>(null);
  const [strategyPriors, setStrategyPriors] = useState<StrategyPrior[]>([]);
  const [evaluationRunning, setEvaluationRunning] = useState(false);
  const [scenarioRunning, setScenarioRunning] = useState<string | null>(null);
  const [demoTourRunning, setDemoTourRunning] = useState(false);
  const [demoTourStep, setDemoTourStep] = useState("");
  const [graphLoading, setGraphLoading] = useState(false);
  const [booting, setBooting] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAllRecoveries, setShowAllRecoveries] = useState(false);
  const evaluationRef = useRef<HTMLElement | null>(null);
  const graphRef = useRef<HTMLElement | null>(null);
  const agentTraceRef = useRef<HTMLElement | null>(null);
  const demoTourCancelled = useRef(false);

  const refresh = async (): Promise<Recovery[]> => {
    try {
      const responses = await Promise.all([
        fetch(`${API}/api/payments/overview`), fetch(`${API}/api/payments/`),
        fetch(`${API}/api/simulator/scenarios/curated`), fetch(`${API}/api/evaluation/runs`),
      ]);
      if (responses.some(response => !response.ok)) throw new Error("One or more dashboard services did not respond.");
      const [metricsBody, recoveryBody, scenarioBody, runsBody] = await Promise.all(responses.map(response => response.json()));
      setMetrics(metricsBody);
      const latestRecoveries = recoveryBody.data ?? [];
      setRecoveries(latestRecoveries);
      setScenarios(scenarioBody.scenarios ?? []);
      const completed = (runsBody.data ?? []).find((run: { status: string; summary?: EvaluationSummary }) => run.status === "completed" && run.summary);
      if (completed) setEvaluation(completed.summary);
      setError(null);
      return latestRecoveries;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The local API is unavailable.");
      return [];
    } finally {
      setBooting(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  const inspectRecovery = async (row: Recovery, scrollToGraph = false) => {
    setSelected(row);
    setGraph(null);
    setGraphLoading(true);
    try {
      const response = await fetch(`${API}/api/decisions/${row.id}/graph`);
      if (!response.ok) throw new Error("Decision graph could not be loaded.");
      setGraph(await response.json());
      if (scrollToGraph) window.setTimeout(() => graphRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }), 120);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Decision graph could not be loaded.");
    } finally {
      setGraphLoading(false);
    }
  };

  const runScenario = async (id: string, modeOverride?: DecisionMode) => {
    const mode = modeOverride ?? decisionMode;
    setScenarioRunning(id);
    setAgentTrace(null);
    setStrategyPriors([]);
    setError(null);
    setMessage(mode === "agent" ? "Waggle is validating memory before Qwen proposes an action…" : "Replaying the scenario through the deterministic recovery engine…");
    try {
      const response = await fetch(`${API}/api/simulator/scenario/${id}/run?decision_mode=${mode}`, { method: "POST" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Scenario failed.");
      if (body.result?.agent_trace) setAgentTrace(body.result.agent_trace);
      setStrategyPriors(body.result?.strategy_priors ?? []);
      const fallback = body.result?.agent_trace?.agent_fallback;
      setMessage(`${body.scenario.name} complete · ${actionSummary(body.result.decision.action, body.result.decision.retry_after_seconds, body.result.decision.recommended_method)}${fallback ? " · safe deterministic fallback used" : mode === "agent" ? " · passed Policy Guard" : ""}`);
      const latestRecoveries = await refresh();
      const current = latestRecoveries.find(row => row.id === body.result?.failure_id);
      if (current) await inspectRecovery(current);
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : "Scenario failed.";
      setError(detail);
      setMessage("Scenario stopped before a decision was recorded.");
    } finally {
      setScenarioRunning(null);
    }
  };

  const runEvaluation = async () => {
    setEvaluationRunning(true);
    setError(null);
    setMessage("Running three systems against the same 200 seeded payment histories…");
    try {
      const response = await fetch(`${API}/api/evaluation/run`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ seed: 42, count: 200 }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Evaluation failed.");
      for (let attempt = 0; attempt < 120; attempt += 1) {
        await new Promise(resolve => window.setTimeout(resolve, 1000));
        const runResponse = await fetch(`${API}/api/evaluation/runs/${body.run_id}`);
        if (!runResponse.ok) continue;
        const run = await runResponse.json();
        if (run.status === "completed") {
          setEvaluation(run.summary);
          setMessage(`Evaluation complete · ${run.summary.scenario_count} scenarios · every decision persisted for audit.`);
          window.setTimeout(() => evaluationRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }), 150);
          return;
        }
        if (run.status === "failed") throw new Error(run.summary?.error ?? "Evaluation failed.");
      }
      setMessage(`Evaluation ${body.run_id} is still running. Refresh later to load its result.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Evaluation failed.");
      setMessage("Evaluation stopped before a result was recorded.");
    } finally {
      setEvaluationRunning(false);
    }
  };

  const pauseTour = async (milliseconds: number) => {
    for (let elapsed = 0; elapsed < milliseconds && !demoTourCancelled.current; elapsed += 250) {
      await new Promise(resolve => window.setTimeout(resolve, 250));
    }
  };

  const showTourSection = async (element: HTMLElement | null, step: string, holdMs: number) => {
    if (demoTourCancelled.current) return;
    setDemoTourStep(step);
    element?.scrollIntoView({ behavior: "smooth", block: "center" });
    await pauseTour(holdMs);
  };

  const runDemoTour = async () => {
    if (demoTourRunning || booting) return;
    demoTourCancelled.current = false;
    setDemoTourRunning(true);
    setError(null);
    try {
      await showTourSection(document.getElementById("overview"), "Opening: the recovery safety promise", 3500);

      setDecisionMode("agent");
      await showTourSection(document.getElementById("simulator"), "Stale Card Trap: Qwen proposes, Policy Guard decides", 1600);
      if (demoTourCancelled.current) return;
      await runScenario("curated_003", "agent");
      await pauseTour(7000);

      await showTourSection(agentTraceRef.current, "Constrained Qwen trace: candidate → policy → recorded action", 6000);
      await showTourSection(graphRef.current, "Memory graph: old card evidence was retained, but rejected as stale", 6500);

      setDecisionMode("deterministic");
      await showTourSection(document.getElementById("simulator"), "Timing Memory: exact validated evidence can be reused", 1000);
      if (demoTourCancelled.current) return;
      await runScenario("curated_002", "deterministic");
      await pauseTour(5500);

      await showTourSection(document.getElementById("simulator"), "Human Escalation: the autonomous boundary is enforced", 1000);
      if (demoTourCancelled.current) return;
      await runScenario("curated_006", "agent");
      await pauseTour(6500);
      await showTourSection(graphRef.current, "Policy BLOCK → ESCALATE → human review; no money movement", 5000);

      if (!evaluation) await runEvaluation();
      await showTourSection(evaluationRef.current, "200 seeded, deterministic, simulated scenarios — zero Groq calls", 8000);

      await showTourSection(document.getElementById("overview"), "Waggle Recover: revenue recovery with a memory of what changed", 6000);
      setMessage("Demo tour complete · all proof points were shown.");
    } finally {
      setDemoTourRunning(false);
      setDemoTourStep("");
    }
  };

  const stopDemoTour = () => {
    demoTourCancelled.current = true;
    setDemoTourStep("Demo tour stopped — you have control again.");
    setMessage("Demo tour stopped.");
  };

  const systems = evaluation ? [evaluation.systems.baseline_a, evaluation.systems.baseline_b, evaluation.systems.system_c] : [];
  const rejectedEvidence = selected?.discarded_json ?? [];
  const staleInstrument = rejectedEvidence.find(item => item.metadata?.instrument_id)?.metadata?.instrument_id;
  const rejectedRetrySeconds = rejectedEvidence.find(item => item.metadata?.retry_after_seconds)?.metadata?.retry_after_seconds;
  const hasRejectedMemory = Boolean(staleInstrument && selected?.instrument_id);
  const escalationRequired = Boolean(selected && (selected.action === "ESCALATE" || selected.human_review_required));
  const visibleRecoveries = showAllRecoveries ? recoveries : recoveries.slice(0, 10);

  return <div className="app-shell">
    <nav className="topbar" aria-label="Primary navigation">
      <a className="brand" href="#overview"><BrandMark /><span>Waggle <b>Recover</b></span></a>
      <div className="nav-links"><a href="#simulator">Simulator</a><a href="#memory">Memory graph</a><a href="#benchmark">Benchmark</a><a href="#activity">Activity</a></div>
      <span className={`service-status ${error ? "offline" : ""}`}><i />{error ? "Needs attention" : "Local system live"}</span>
    </nav>
    {demoTourRunning && <div className="demo-tour-status" role="status" aria-live="polite"><span className="spinner" /> <b>Demo tour running</b><span>{demoTourStep}</span><button onClick={stopDemoTour}>Stop tour</button></div>}

    <main>
      <section className="hero" id="overview">
        <div className="hero-copy">
          <p className="eyebrow">Payment recovery control room</p>
          <h1>Recover the right payment.<br /><em>Ignore the wrong memory.</em></h1>
          <p className="lede">Waggle connects payment failures, instrument changes, recovery outcomes, and merchant policy—then proves which memories were safe to use.</p>
          <div className="hero-actions">
            <button className="primary-action" disabled={Boolean(scenarioRunning) || booting || demoTourRunning} onClick={() => void runScenario("curated_003")}>
              {scenarioRunning === "curated_003" ? <><span className="spinner dark" /> Running stale-card trap</> : <>Run stale-card trap <span aria-hidden="true">→</span></>}
            </button>
            <button className="demo-tour-button" disabled={booting} onClick={() => demoTourRunning ? stopDemoTour() : void runDemoTour()}>{demoTourRunning ? "Stop demo tour" : "Record demo tour"}<span aria-hidden="true">◉</span></button>
            <a className="secondary-action" href="#memory">See how memory is validated</a>
          </div>
        </div>
        <aside className="decision-map" aria-label="How Waggle decides">
          <div className="map-header"><span>Decision protocol</span><b>3 guarded steps</b></div>
          <ol>
            <li><span>01</span><div><strong>Retrieve</strong><small>Find customer, merchant, instrument, and failure history.</small></div></li>
            <li><span>02</span><div><strong>Validate time</strong><small>Reject evidence invalidated by replacement or expiry.</small></div></li>
            <li><span>03</span><div><strong>Act within policy</strong><small>Choose a bounded recovery action and record its outcome.</small></div></li>
          </ol>
          <div className="map-footer"><i />Policy Guard remains final authority</div>
        </aside>
      </section>

      <div className={`status-line ${error ? "error" : ""}`} role="status" aria-live="polite"><span>{error ? "!" : "↳"}</span><p>{error ?? message}</p>{error && <button onClick={() => void refresh()}>Retry connection</button>}</div>

      <section className="metric-rail" aria-label="Recovery overview">
        {booting ? Array.from({ length: 5 }, (_, index) => <div className="metric skeleton" key={index} />) : <>
          <div className="metric"><span>GMV at risk</span><strong>{money(metrics?.gmv_at_risk)}</strong><small>across recorded failures</small></div>
          <div className="metric featured"><span>Recovered GMV</span><strong>{money(metrics?.recovered_gmv)}</strong><small>{metrics?.recovery_rate_pct ?? 0}% of at-risk value</small></div>
          <div className="metric"><span>Stale evidence blocked</span><strong>{metrics?.stale_evidence_prevented ?? 0}</strong><small>unsafe memories excluded</small></div>
          <div className="metric"><span>Policy violations</span><strong>{metrics?.policy_violations ?? 0}</strong><small>{metrics?.policy_violations ? "requires review" : "guardrails intact"}</small></div>
          <div className="metric policy"><span>Recovery policy</span><strong>{metrics?.policy_violations ? "Review" : "Clear"}</strong><small><i /> deterministic enforcement</small></div>
        </>}
      </section>

      <section className="workspace-section" id="simulator">
        <div className="section-heading"><div><p className="section-index">01 / SIMULATOR</p><h2>Replay the cases that break naive recovery.</h2></div><p>Each scenario enters through the same normalized pipeline as a Razorpay webhook. Only the simulator can switch decision providers.</p></div>
        <div className="workspace-grid">
          <article className="scenario-console">
            <div className="console-toolbar">
              <div><span>Decision provider</span><small>Choose how a candidate action is proposed</small></div>
              <div className="mode-switch" role="group" aria-label="Decision provider">
                <button disabled={demoTourRunning} aria-pressed={decisionMode === "deterministic"} className={decisionMode === "deterministic" ? "active" : ""} onClick={() => { setDecisionMode("deterministic"); setAgentTrace(null); }}>Rules</button>
                <button disabled={demoTourRunning} aria-pressed={decisionMode === "agent"} className={decisionMode === "agent" ? "active" : ""} onClick={() => setDecisionMode("agent")}>Qwen agent</button>
              </div>
            </div>
            <div className="scenario-list">
              {booting && Array.from({ length: 5 }, (_, index) => <div className="scenario-row skeleton" key={index} />)}
              {scenarios.map(scenario => {
                const copy = scenarioCopy[scenario.id] ?? { index: "—", description: scenario.category?.replaceAll("_", " ") ?? "Recovery scenario" };
                const running = scenarioRunning === scenario.id;
                return <button className={`scenario-row ${scenario.has_stale_memory ? "hero-scenario" : ""}`} key={scenario.id} disabled={Boolean(scenarioRunning) || demoTourRunning} onClick={() => void runScenario(scenario.id)}>
                  <span className="scenario-number">{copy.index}</span><span className="scenario-text"><strong>{scenario.name}</strong><small>{copy.description}</small></span>
                  {scenario.has_stale_memory && <span className="scenario-tag">Hero case</span>}
                  <span className="run-icon" aria-hidden="true">{running ? <span className="spinner" /> : "→"}</span>
                </button>;
              })}
            </div>
            <p className="console-note"><span>i</span>{decisionMode === "agent" ? "Qwen proposes from validated context; deterministic policy still decides." : "Rules mode is reproducible and powers the benchmark."}</p>
          </article>

          <article className="inspector" aria-live="polite">
            <div className="inspector-header"><div><span>Decision inspector</span><small>{selected ? `Failure ${selected.id.slice(0, 12)}` : "Awaiting a completed scenario"}</small></div>{selected && <span className={`outcome-chip ${escalationRequired ? "failure" : selected.outcome?.toLowerCase()}`}>{escalationRequired ? "HUMAN REVIEW" : selected.outcome ?? "PENDING"}</span>}</div>
            {selected ? <>
              {hasRejectedMemory && <div className="stale-alert"><span className="alert-kicker">Waggle safety intervention</span><strong>Stale memory rejected</strong><span className="instrument-swap"><b>{staleInstrument}</b><i>→</i><b>{selected.instrument_id}</b></span><p>Historical {rejectedRetrySeconds ? `${Math.round(rejectedRetrySeconds / 60)}-minute retry` : "recovery"} belonged to the replaced card and could not drive this decision.</p></div>}
              {escalationRequired && <div className="escalation-alert"><span className="alert-kicker">Autonomous boundary enforced</span><strong>Human review required</strong><p><b>Automated recovery stopped</b><br />Reason: {selected.escalation_reason || "Maximum automated recovery attempts reached"}<br />Attempts used: {selected.attempt_count ?? 0} / {selected.max_automated_attempts ?? 3}<br />Policy result: {selected.policy_result || "BLOCK"}<br />Money movement: <b>NONE</b></p><small>Recommended next step: Manual review / customer outreach</small></div>}
              <div className="decision-flow">
                <div className="decision-step failure-step"><span>Failure</span><strong>{selected.failure_code}</strong><small>{selected.method} · {money(selected.amount)}</small></div>
                <div className={`flow-link ${hasRejectedMemory ? "rejected" : escalationRequired ? "blocked" : ""}`}><span>{hasRejectedMemory ? "rejected" : escalationRequired ? "blocked" : "validated"}</span></div>
                <div className={`decision-step ${hasRejectedMemory ? "rejected-step" : escalationRequired ? "blocked-step" : "evidence-step"}`}><span>{escalationRequired ? "Policy Guard" : "Evidence"}</span><strong>{escalationRequired ? "BLOCK" : hasRejectedMemory ? "Not current" : "Validated"}</strong><small>{escalationRequired ? `${selected.attempt_count ?? 0}/${selected.max_automated_attempts ?? 3} attempts used` : hasRejectedMemory ? `${staleInstrument} superseded` : "exact scope only"}</small></div>
                <div className="flow-link"><span>policy</span></div>
                <div className={`decision-step action-step ${escalationRequired ? "escalation-step" : ""}`}><span>Final action</span><strong>{actionSummary(selected.action, selected.retry_after_seconds, selected.recommended_method)}</strong><small>{escalationRequired ? "Human review · no payment action" : selected.outcome ?? "pending outcome"}</small></div>
              </div>
              <div className="audit-row"><details><summary>Decision explanation</summary><p>{selected.explanation || "No explanation recorded."}</p></details><details><summary>Evidence audit · {rejectedEvidence.length} rejected</summary><pre>{JSON.stringify(rejectedEvidence, null, 2)}</pre></details></div>
            </> : <div className="inspector-empty"><div className="empty-glyph"><span>F</span><i /><span>M</span><i /><span>D</span></div><strong>No decision selected</strong><p>Run <b>Stale Card Trap</b> to see an old success retrieved, invalidated, and excluded before the final action.</p></div>}
          </article>
        </div>
      </section>

      {agentTrace && <section ref={agentTraceRef} className={`agent-trace ${agentTrace.agent_fallback ? "fallback-trace" : ""}`}><div className="trace-heading"><div><p className="section-index">CONSTRAINED MODEL TRACE</p><h2>Qwen proposes. Policy decides.</h2></div><div className="model-chip"><span>Groq · {agentTrace.model}</span><small>{agentTrace.model_latency_ms} ms</small></div></div>{agentTrace.agent_fallback && <div className="fallback-banner"><strong>Safe fallback used</strong><span>{agentTrace.fallback_reason}</span></div>}<div className="trace-stages">{agentTrace.stages.map((stage, index) => <article key={`${stage.key}-${index}`} className={`trace-stage ${stage.status}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{stage.label}</strong><p>{stage.detail}</p></div></article>)}</div><div className="trace-result"><div><span>Model candidate</span><strong>{actionSummary(agentTrace.candidate_action, agentTrace.candidate_retry_after_seconds, agentTrace.candidate_recommended_method)}</strong><small>{agentTrace.candidate_reason}</small></div><i>→</i><div><span>Policy Guard</span><strong>{agentTrace.policy_result ?? "—"}</strong><small>deterministic constraints</small></div><i>→</i><div><span>Recorded action</span><strong>{actionSummary(agentTrace.final_action, agentTrace.final_retry_after_seconds, agentTrace.final_recommended_method)}</strong><small>model never moves money</small></div></div></section>}

      {strategyPriors.length > 0 && <section className="strategy-memory"><div className="section-heading compact"><div><p className="section-index">ADAPTIVE MEMORY</p><h2>Merchant evidence, bounded by policy.</h2></div><p>Recency-weighted estimates from authoritative outcomes. Superseded evidence has zero weight.</p></div><div className="strategy-grid">{strategyPriors.slice(0, 3).map(prior => <article key={`${prior.action}-${prior.recommended_method ?? ""}`}><div><strong>{actionSummary(prior.action, null, prior.recommended_method)}</strong><span className={prior.insufficient_history ? "prior-warming" : "prior-ready"}>{prior.insufficient_history ? "warming" : "eligible"}</span></div><b>{Math.round(prior.posterior_success_probability * 100)}%</b><small>posterior · effective n={prior.effective_n.toFixed(1)}</small><small>{prior.authoritative_evidence_ids.length} authoritative outcomes</small></article>)}</div></section>}

      <section className="graph-section" id="memory" ref={graphRef}>
        <div className="section-heading"><div><p className="section-index">02 / MEMORY GRAPH</p><h2>The decision is a path, not a paragraph.</h2></div><p>Waggle keeps old evidence for audit, marks when it stopped being valid, and connects the exact evidence path behind every recovery action.</p></div>
        <GraphView graph={graph} loading={graphLoading} />
      </section>

      <section className="evaluation-section" id="benchmark" ref={evaluationRef}>
        <div className="evaluation-heading"><div><p className="section-index">03 / BENCHMARK</p><h2>Same histories. Three recovery systems.</h2><p>Seed 42 · parameter-aware scoring · deterministic outcome model · no Groq calls</p></div><button className="primary-action" disabled={evaluationRunning} onClick={() => void runEvaluation()}>{evaluationRunning ? <><span className="spinner dark" /> Evaluating 200 cases</> : <>Run 200-case evaluation <span>→</span></>}</button></div>
        {evaluation ? <>
          <div className="proof-strip"><div><span>Waggle action accuracy</span><strong>{evaluation.systems.system_c.action_accuracy_pct}%</strong></div><div><span>Recovery success</span><strong>{evaluation.systems.system_c.success_rate_pct}%</strong></div><div><span>Simulated GMV recovery</span><strong>{evaluation.systems.system_c.recovery_rate_gmv_pct}%</strong></div><div><span>Exact stale rejection</span><strong>{evaluation.systems.system_c.stale_rejection_rate_pct}%</strong></div></div>
          <div className="table-frame benchmark-table"><table><thead><tr><th>System</th><th>Action accuracy</th><th>Recovery success</th><th>GMV recovery</th><th>Stale rejection</th><th>Latency</th></tr></thead><tbody>{systems.map(system => <tr key={system.name} className={system === evaluation.systems.system_c ? "system-c" : ""}><td data-label="System"><span className="system-marker" />{system.name}</td><td data-label="Action accuracy">{system.action_accuracy_pct}%</td><td data-label="Recovery success">{system.success_rate_pct}%</td><td data-label="GMV recovery">{system.recovery_rate_gmv_pct}%</td><td data-label="Stale rejection">{system.stale_rejection_rate_pct}%</td><td data-label="Latency">{system.avg_latency_ms} ms</td></tr>)}</tbody></table></div>
          <p className="benchmark-note">Success can remain below action accuracy because some correctly selected safe actions intentionally stop recovery.</p>
        </> : <div className="benchmark-empty"><strong>No completed benchmark loaded</strong><span>Run the seeded evaluation to compare blind retry, contextual history, and supersession-aware Waggle Recover.</span></div>}
      </section>

      <section className="activity-section" id="activity">
        <div className="section-heading compact"><div><p className="section-index">04 / ACTIVITY</p><h2>Recent recovery decisions</h2></div><p>{recoveries.length ? `Showing ${showAllRecoveries ? "all" : `latest ${Math.min(10, recoveries.length)} of`} ${recoveries.length} recorded failures.` : "Run a scenario to create the first auditable recovery."}</p></div>
        <div className="table-frame activity-table"><table><thead><tr><th>Customer</th><th>Amount</th><th>Failure</th><th>Decision</th><th>Outcome</th><th><span className="sr-only">Action</span></th></tr></thead><tbody>{visibleRecoveries.map((row, index) => <tr key={`${row.id}-${row.action ?? "pending"}-${row.outcome ?? "pending"}-${index}`}><td data-label="Customer"><b>{row.customer_id}</b><small>{row.method} · {row.instrument_id ?? "instrument unknown"}</small></td><td data-label="Amount">{money(row.amount)}</td><td data-label="Failure"><span className="failure-code">{row.failure_code}</span></td><td data-label="Decision">{actionSummary(row.action, row.retry_after_seconds, row.recommended_method)}</td><td data-label="Outcome"><span className={`outcome-text ${row.action === "ESCALATE" ? "escalated" : row.outcome?.toLowerCase()}`}>{row.action === "ESCALATE" ? "HUMAN REVIEW" : row.outcome ?? "PENDING"}</span>{row.action === "ESCALATE" ? <small>no money movement</small> : row.recovered_amount ? <small>{money(row.recovered_amount)} recovered</small> : null}</td><td><button className="text-action" onClick={() => void inspectRecovery(row, true)}>Open graph <span>→</span></button></td></tr>)}{!recoveries.length && <tr><td colSpan={6}><div className="table-empty">No recoveries yet. The first scenario will appear here with its decision and outcome.</div></td></tr>}</tbody></table></div>
        {recoveries.length > 10 && <button className="show-more" onClick={() => setShowAllRecoveries(value => !value)}>{showAllRecoveries ? "Show latest 10" : `Show all ${recoveries.length} recoveries`}</button>}
      </section>
    </main>
    <footer><div className="brand"><BrandMark /><span>Waggle Recover</span></div><p>Prototype recovery decisions only. Razorpay webhooks remain policy-controlled.</p><a href="#overview">Back to top ↑</a></footer>
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);
