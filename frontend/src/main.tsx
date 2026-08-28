import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
type Metrics = { gmv_at_risk: number; recovered_gmv: number; recovery_rate_pct: number; stale_evidence_prevented: number; policy_violations: number };
type Evidence = { rejection_reason?: string; metadata?: { instrument_id?: string; retry_after_seconds?: number } };
type Recovery = { id: string; customer_id: string; amount: number; method: string; instrument_id?: string; failure_code: string; action?: string; recommended_method?: string; retry_after_seconds?: number; outcome?: string; recovered_amount?: number; explanation?: string; discarded_json?: Evidence[] };
type SystemMetrics = { name: string; action_accuracy_pct: number; success_rate_pct: number; recovery_rate_gmv_pct: number; stale_rejection_rate_pct: number; avg_latency_ms: number };
type EvaluationSummary = { scenario_count: number; systems: Record<"baseline_a" | "baseline_b" | "system_c", SystemMetrics> };
type GraphNode = { id: string; label: string; node_type: string; tags?: string[]; metadata?: Record<string, unknown>; valid_to?: string | null };
type GraphEdge = { source_id: string; target_id: string; relationship: string; metadata?: Record<string, unknown> };
type MemoryGraph = { root_id?: string; nodes: GraphNode[]; edges: GraphEdge[]; message?: string };
type DecisionMode = "deterministic" | "agent";
type AgentStage = { key: string; label: string; status: "complete" | "warning" | "fallback"; detail: string };
type AgentTrace = {
  decision_mode: "agent";
  model_provider: string;
  model: string;
  candidate_action?: string;
  candidate_retry_after_seconds?: number | null;
  candidate_recommended_method?: string | null;
  candidate_reason?: string;
  policy_result?: string;
  final_action?: string;
  final_retry_after_seconds?: number | null;
  final_recommended_method?: string | null;
  agent_fallback: boolean;
  fallback_reason?: string | null;
  model_latency_ms: number;
  stages: AgentStage[];
};
type StrategyPrior = {
  action: string;
  recommended_method?: string | null;
  posterior_success_probability: number;
  global_prior: number;
  effective_n: number;
  insufficient_history: boolean;
  selected_bucket: string;
  authoritative_evidence_ids: string[];
};

const money = (paise = 0) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(paise / 100);
const actionSummary = (action?: string | null, retry?: number | null, method?: string | null) =>
  `${action ?? "—"}${retry != null ? ` after ${retry}s` : method ? ` → ${method.toUpperCase()}` : ""}`;

function GraphView({ graph }: { graph: MemoryGraph | null }) {
  if (!graph?.nodes.length) return <div className="graph-empty"><strong>No graph selected</strong><span>Select a completed recovery to reveal the Waggle memory trail.</span></div>;

  const uniqueNodes = Array.from(new Map(graph.nodes.map(node => [node.id, node])).values());
  const rootId = graph.root_id ?? uniqueNodes.find(node => node.tags?.includes("recovery_decision"))?.id;
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

  const rejectedNodes = uniqueNodes
    .filter(node => roleFor(node) === "rejected")
    .sort((a, b) => Number(a.metadata?.retry_after_seconds === 480) - Number(b.metadata?.retry_after_seconds === 480))
    .reverse()
    .slice(0, 1);
  const essentialIds = new Set([
    rootId, currentFailureId, currentOutcomeId,
    ...rejectedNodes.map(node => node.id),
    ...uniqueNodes.filter(node => ["superseded", "current-instrument"].includes(roleFor(node))).map(node => node.id),
  ].filter(Boolean));
  const nodes = uniqueNodes.filter(node => essentialIds.has(node.id)).slice(0, 7);
  const roleCounts: Record<string, number> = {};
  const compact = window.innerWidth < 850;
  const slots: Record<string, Array<{ x: number; y: number }>> = compact ? {
    superseded: [{ x: 95, y: 75 }],
    "current-instrument": [{ x: 285, y: 75 }],
    rejected: [{ x: 95, y: 245 }],
    failure: [{ x: 285, y: 245 }],
    decision: [{ x: 190, y: 410 }],
    outcome: [{ x: 190, y: 550 }],
    accepted: [{ x: 95, y: 245 }],
    memory: [{ x: 285, y: 410 }],
  } : {
    failure: [{ x: 110, y: 92 }],
    rejected: [{ x: 110, y: 285 }, { x: 110, y: 375 }],
    superseded: [{ x: 330, y: 350 }],
    "current-instrument": [{ x: 535, y: 350 }],
    decision: [{ x: 420, y: 130 }],
    outcome: [{ x: 700, y: 130 }],
    accepted: [{ x: 225, y: 245 }],
    memory: [{ x: 655, y: 300 }],
  };
  const positions = new Map(nodes.map(node => {
    const role = roleFor(node);
    const index = roleCounts[role] ?? 0;
    roleCounts[role] = index + 1;
    return [node.id, slots[role]?.[index] ?? { x: 680, y: 300 + index * 78 }] as const;
  }));
  const storyRelations = new Set(["updates", "decision_for_failure", "outcome_of_decision", "rejected_evidence", "accepted_evidence"]);
  const edges = graph.edges.filter(edge => positions.has(edge.source_id) && positions.has(edge.target_id) && storyRelations.has(String(edge.metadata?.relation ?? edge.relationship)));

  const titleFor = (node: GraphNode) => {
    const role = roleFor(node);
    if (role === "failure") return "Current payment failed";
    if (role === "decision") {
      const retry = Number(node.metadata?.retry_after_seconds ?? 0);
      return retry ? `Retry after ${retry}s` : `Suggest ${String(node.metadata?.recommended_method ?? "method").toUpperCase()}`;
    }
    if (role === "outcome") return `${String(node.metadata?.outcome ?? "Recovery")} outcome`;
    if (role === "rejected") return "Old success memory";
    if (["superseded", "current-instrument"].includes(role)) return String(node.metadata?.alias ?? node.label);
    return node.label.replace(/^Recovery /, "").slice(0, 24);
  };
  const detailFor = (node: GraphNode) => {
    const role = roleFor(node);
    if (role === "failure") return `${String(node.metadata?.instrument_id ?? "")} · ${String(node.metadata?.failure_code ?? "")}`;
    if (role === "decision") return "Waggle-validated action";
    if (role === "outcome") return Number(node.metadata?.recovered_amount) ? `${money(Number(node.metadata?.recovered_amount))} recovered` : "Result captured";
    if (role === "rejected") {
      const seconds = Number(node.metadata?.retry_after_seconds ?? 0);
      return `${String(node.metadata?.instrument_id ?? "old card")}${seconds ? ` · retry ${Math.round(seconds / 60)} min` : ""}`;
    }
    if (role === "superseded") return "SUPERSEDED CARD";
    if (role === "current-instrument") return "CURRENT CARD";
    return node.node_type.toUpperCase();
  };
  const edgeLabel = (edge: GraphEdge) => {
    const semantic = String(edge.metadata?.relation ?? edge.relationship);
    return ({ decision_for_failure: "FAILURE → DECISION", outcome_of_decision: "PRODUCED", rejected_evidence: "REJECTED AS STALE", accepted_evidence: "USED AS MEMORY", updates: "SUPERSEDES" } as Record<string, string>)[semantic] ?? semantic.replaceAll("_", " ").toUpperCase();
  };

  return <div className="graph-wrap">
    <div className="graph-stats"><span><b>{uniqueNodes.length}</b> connected nodes</span><span className="rejected-stat"><b>{rejectedIds.size}</b> memories rejected as stale</span><span><b>1</b> explainable decision</span></div>
    <svg viewBox={compact ? "0 0 380 625" : "0 0 820 440"} role="img" aria-label="Waggle decision memory graph">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>
      {edges.map((edge, index) => {
        const semantic = String(edge.metadata?.relation ?? edge.relationship);
        const reverseForStory = ["decision_for_failure", "outcome_of_decision", "rejected_evidence", "updates"].includes(semantic);
        const source = positions.get(reverseForStory ? edge.target_id : edge.source_id)!;
        const target = positions.get(reverseForStory ? edge.source_id : edge.target_id)!;
        const labelY = semantic === "updates" ? source.y - 52 : (source.y + target.y) / 2 + (semantic === "decision_for_failure" ? 10 : semantic === "rejected_evidence" ? -15 : -7);
        return <g key={`${edge.source_id}-${edge.target_id}-${index}`}>
          <line className={`edge ${edge.metadata?.validation_status === "rejected" ? "rejected-edge" : ""}`} x1={source.x} y1={source.y} x2={target.x} y2={target.y} markerEnd="url(#arrow)" />
          <text className="edge-label" textAnchor="middle" x={(source.x + target.x) / 2} y={labelY}>{edgeLabel(edge)}</text>
        </g>;
      })}
      {nodes.map(node => {
        const position = positions.get(node.id)!;
        const role = roleFor(node);
        return <g className={`graph-node ${role}`} key={node.id} transform={`translate(${position.x} ${position.y})`}>
          <rect x="-82" y="-39" width="164" height="78" rx="12" />
          <text className="node-label" textAnchor="middle" y="-7">{titleFor(node).slice(0, 25)}</text>
          <text className="node-type" textAnchor="middle" y="14">{detailFor(node).slice(0, 31)}</text>
        </g>;
      })}
    </svg>
    <div className="graph-legend"><span><i className="memory-dot" /> Stored memory/event</span><span><i className="stale-dot" /> Rejected or superseded</span><span><i className="decision-dot" /> Final decision/outcome</span></div>
  </div>;
}

function App() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [recoveries, setRecoveries] = useState<Recovery[]>([]);
  const [selected, setSelected] = useState<Recovery | null>(null);
  const [graph, setGraph] = useState<MemoryGraph | null>(null);
  const [evaluation, setEvaluation] = useState<EvaluationSummary | null>(null);
  const [scenarios, setScenarios] = useState<{ id: string; name: string }[]>([]);
  const [message, setMessage] = useState("Connect the simulator to see live decisions.");
  const [decisionMode, setDecisionMode] = useState<DecisionMode>("deterministic");
  const [agentTrace, setAgentTrace] = useState<AgentTrace | null>(null);
  const [strategyPriors, setStrategyPriors] = useState<StrategyPrior[]>([]);
  const [evaluationRunning, setEvaluationRunning] = useState(false);
  const evaluationRef = useRef<HTMLElement | null>(null);
  const graphRef = useRef<HTMLElement | null>(null);

  const refresh = async (): Promise<Recovery[]> => {
    const [metricsResponse, recoveryResponse, scenarioResponse, runsResponse] = await Promise.all([
      fetch(`${API}/api/payments/overview`), fetch(`${API}/api/payments/`),
      fetch(`${API}/api/simulator/scenarios/curated`), fetch(`${API}/api/evaluation/runs`),
    ]);
    if (metricsResponse.ok) setMetrics(await metricsResponse.json());
    let latestRecoveries: Recovery[] = [];
    if (recoveryResponse.ok) {
      latestRecoveries = (await recoveryResponse.json()).data ?? [];
      setRecoveries(latestRecoveries);
    }
    if (scenarioResponse.ok) setScenarios((await scenarioResponse.json()).scenarios ?? []);
    if (runsResponse.ok) {
      const completed = ((await runsResponse.json()).data ?? []).find((run: { status: string; summary?: EvaluationSummary }) => run.status === "completed" && run.summary);
      if (completed) setEvaluation(completed.summary);
    }
    return latestRecoveries;
  };

  useEffect(() => { void refresh(); }, []);

  const runScenario = async (id: string) => {
    setAgentTrace(null);
    setStrategyPriors([]);
    setMessage(decisionMode === "agent" ? "Waggle is validating memory before Qwen reasons…" : "Running deterministic scenario…");
    const response = await fetch(`${API}/api/simulator/scenario/${id}/run?decision_mode=${decisionMode}`, { method: "POST" });
    const body = await response.json();
    if (body.result?.agent_trace) setAgentTrace(body.result.agent_trace);
    setStrategyPriors(body.result?.strategy_priors ?? []);
    const fallback = body.result?.agent_trace?.agent_fallback;
    setMessage(response.ok ? `${body.scenario.name}: ${body.result.decision.action}${fallback ? " · AI unavailable, safe deterministic fallback used" : decisionMode === "agent" ? " · Qwen candidate passed through Policy Guard" : ""}` : body.detail ?? "Scenario failed");
    const latestRecoveries = await refresh();
    const current = latestRecoveries.find(row => row.id === body.result?.failure_id);
    if (current) await inspectRecovery(current);
  };

  const inspectRecovery = async (row: Recovery, scrollToGraph = false) => {
    setSelected(row);
    setGraph(null);
    const response = await fetch(`${API}/api/decisions/${row.id}/graph`);
    if (response.ok) {
      setGraph(await response.json());
      if (scrollToGraph) window.setTimeout(() => graphRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }), 100);
    }
  };

  const runEvaluation = async () => {
    setEvaluationRunning(true);
    setMessage("Evaluating 200 seeded synthetic histories…");
    const response = await fetch(`${API}/api/evaluation/run`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ seed: 42, count: 200 }) });
    const body = await response.json();
    if (!response.ok) { setMessage(body.detail ?? "Evaluation failed"); setEvaluationRunning(false); return; }
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 1000));
      const runResponse = await fetch(`${API}/api/evaluation/runs/${body.run_id}`);
      if (!runResponse.ok) continue;
      const run = await runResponse.json();
      if (run.status === "completed") {
        setEvaluation(run.summary);
        setEvaluationRunning(false);
        setMessage(`Evaluation complete — ${run.summary.scenario_count} scenarios, with every system decision persisted.`);
        window.setTimeout(() => evaluationRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }), 150);
        return;
      }
      if (run.status === "failed") { setMessage(`Evaluation failed: ${run.summary?.error ?? "unknown error"}`); setEvaluationRunning(false); return; }
    }
    setEvaluationRunning(false);
    setMessage(`Evaluation ${body.run_id} is still running. Its results will appear after refresh.`);
  };

  const cards = [["GMV at risk", money(metrics?.gmv_at_risk)], ["Recovered GMV", money(metrics?.recovered_gmv)], ["Recovery rate", `${metrics?.recovery_rate_pct ?? 0}%`], ["Stale evidence prevented", metrics?.stale_evidence_prevented ?? 0], ["Policy violations", metrics?.policy_violations ?? 0]];
  const systems = evaluation ? [evaluation.systems.baseline_a, evaluation.systems.baseline_b, evaluation.systems.system_c] : [];
  const rejectedEvidence = selected?.discarded_json ?? [];
  const staleInstrument = rejectedEvidence.find(item => item.metadata?.instrument_id)?.metadata?.instrument_id;
  const rejectedRetrySeconds = rejectedEvidence.find(item => item.metadata?.retry_after_seconds)?.metadata?.retry_after_seconds;
  const rejectedRetryLabel = rejectedRetrySeconds ? `${Math.round(rejectedRetrySeconds / 60)}-min retry` : "historical recovery";
  const hasRejectedMemory = Boolean(staleInstrument && selected?.instrument_id);

  return <main>
    <header><div><p className="eyebrow">Waggle Recover</p><h1>Revenue recovery, with a memory of what changed.</h1></div><button disabled={evaluationRunning} onClick={() => void runEvaluation()}>{evaluationRunning ? "Evaluating 200 cases…" : "Run deterministic evaluation"}</button></header>
    <p className="message">{message}</p>
    <section className="cards">{cards.map(([label, value]) => <article key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</section>
    <section className="grid">
      <article className="panel"><h2>Demo scenarios</h2><p>Every run uses the same normalized event pipeline as Razorpay webhooks.</p><div className="mode-control"><span>Decision Mode</span><div><button className={decisionMode === "deterministic" ? "active" : ""} onClick={() => { setDecisionMode("deterministic"); setAgentTrace(null); }}>Deterministic</button><button className={decisionMode === "agent" ? "active" : ""} onClick={() => setDecisionMode("agent")}>AI Agent</button></div><small>{decisionMode === "agent" ? "Qwen proposes; deterministic Policy Guard decides." : "Reproducible rules used for the benchmark."}</small></div><div className="scenarios">{scenarios.map(scenario => <button key={scenario.id} onClick={() => void runScenario(scenario.id)}>{scenario.name}</button>)}</div></article>
      <article className="panel inspector"><h2>Decision inspector</h2>{selected ? <>
        {hasRejectedMemory && <div className="stale-alert">
          <span className="alert-kicker">Waggle safety intervention</span>
          <strong>STALE MEMORY REJECTED</strong>
          <span className="instrument-swap"><b>{staleInstrument}</b><i>→</i><b>{selected.instrument_id}</b></span>
          <p>Old instrument was superseded. Historical {rejectedRetryLabel} was excluded from this decision.</p>
        </div>}
        <div className="decision-flow">
          <div className="decision-step failure-step"><span>1 · Failure</span><strong>{selected.failure_code}</strong><small>{selected.method} · {money(selected.amount)}</small></div>
          <div className={`decision-step ${hasRejectedMemory ? "rejected-step" : "evidence-step"}`}><span>2 · Evidence</span><strong>{hasRejectedMemory ? "Rejected as stale" : "Validated"}</strong><small>{hasRejectedMemory ? `${staleInstrument} no longer current` : "Current context only"}</small></div>
          <div className="decision-step action-step"><span>3 · Final action</span><strong>{selected.action ?? "pending"}</strong><small>{selected.retry_after_seconds != null ? `Retry after ${selected.retry_after_seconds}s` : selected.recommended_method ? `Use ${selected.recommended_method.toUpperCase()}` : selected.outcome ?? "pending"}</small></div>
        </div>
        <details className="audit-details"><summary>View full decision explanation</summary><p className="full-explanation">{selected.explanation || "No explanation recorded."}</p></details>
        <details className="audit-details"><summary>View raw evidence audit ({rejectedEvidence.length} rejected)</summary><pre>{JSON.stringify(rejectedEvidence, null, 2)}</pre></details>
      </> : <p>Select a recovery in the feed to see the evidence audit trail.</p>}</article>
    </section>
    {agentTrace && <section className={`panel agent-trace ${agentTrace.agent_fallback ? "fallback-trace" : ""}`}><div className="trace-heading"><div><p className="eyebrow">Constrained AI, auditable by design</p><h2>AI Agent Trace</h2></div><div className="model-chip"><span>Groq · {agentTrace.model}</span><small>{agentTrace.model_latency_ms} ms</small></div></div>{agentTrace.agent_fallback && <div className="fallback-banner"><strong>SAFE FALLBACK USED</strong><span>{agentTrace.fallback_reason}</span></div>}<div className="trace-stages">{agentTrace.stages.map((stage, index) => <article key={`${stage.key}-${index}`} className={`trace-stage ${stage.status}`}><span className="stage-index">{index + 1}</span><div><strong>{stage.label}</strong><p>{stage.detail}</p></div></article>)}</div><div className="trace-result"><div><span>Candidate Action</span><strong>{actionSummary(agentTrace.candidate_action, agentTrace.candidate_retry_after_seconds, agentTrace.candidate_recommended_method)}</strong><small>{agentTrace.candidate_reason}</small></div><i>→</i><div><span>Policy Guard</span><strong>{agentTrace.policy_result ?? "—"}</strong><small>Deterministic merchant constraints</small></div><i>→</i><div><span>Final Action</span><strong>{actionSummary(agentTrace.final_action, agentTrace.final_retry_after_seconds, agentTrace.final_recommended_method)}</strong><small>The LLM never executes money movement.</small></div></div></section>}
    {strategyPriors.length > 0 && <section className="panel strategy-memory"><div className="strategy-heading"><div><p className="eyebrow">Waggle learns online</p><h2>Adaptive Strategy Memory</h2></div><span className="authority-chip">Policy Guard stays final</span></div><p className="muted">Recency-weighted Bayesian estimates from authoritative recovery outcomes. Stale or superseded evidence contributes zero.</p><div className="strategy-grid">{strategyPriors.slice(0, 3).map(prior => <article key={`${prior.action}-${prior.recommended_method ?? ""}`}><div><strong>{actionSummary(prior.action, null, prior.recommended_method)}</strong><span className={prior.insufficient_history ? "prior-warming" : "prior-ready"}>{prior.insufficient_history ? "warming up" : "eligible"}</span></div><b>{Math.round(prior.posterior_success_probability * 100)}%</b><small>posterior · effective n={prior.effective_n.toFixed(1)} · global {Math.round(prior.global_prior * 100)}%</small><small>{prior.authoritative_evidence_ids.length} authoritative outcome{prior.authoritative_evidence_ids.length === 1 ? "" : "s"}</small></article>)}</div></section>}
    <section className="panel graph-panel" ref={graphRef}><div className="graph-intro"><div><p className="eyebrow">This is Waggle</p><h2>Decision Memory Graph</h2></div><p>Waggle turns payment events into <strong>connected, temporal memory</strong>. For every decision it shows what happened, which history was retrieved, what became stale, and why the final action was chosen.</p></div><GraphView graph={graph} /></section>
    <section className="panel evaluation-panel" ref={evaluationRef}><div className="evaluation-heading"><div><p className="eyebrow">Proof at scale · no model calls</p><h2>Deterministic Policy Evaluation</h2></div>{evaluation && <span className="run-badge">{evaluation.scenario_count} seeded cases</span>}</div>{evaluation ? <><div className="evaluation-proof"><div><strong>{evaluation.systems.system_c.action_accuracy_pct}%</strong><span>parameter-aware action accuracy</span></div><div><strong>{evaluation.systems.system_c.stale_rejection_rate_pct}%</strong><span>exact stale-evidence rejection</span></div></div><p className="muted">Same 200 seeded cases, same outcome model, zero Groq calls. Only System C validates whether remembered evidence is still current.</p><div className="table-frame"><table><thead><tr><th>System</th><th>Action accuracy</th><th>Success rate</th><th>GMV recovery</th><th>Stale rejection</th><th>Avg latency</th></tr></thead><tbody>{systems.map(system => <tr key={system.name} className={system === evaluation.systems.system_c ? "system-c" : ""}><td>{system.name}</td><td>{system.action_accuracy_pct}%</td><td>{system.success_rate_pct}%</td><td>{system.recovery_rate_gmv_pct}%</td><td>{system.stale_rejection_rate_pct}%</td><td>{system.avg_latency_ms} ms</td></tr>)}</tbody></table></div></> : <p className="muted">Run the reproducible benchmark to compare blind retry, history-only recovery, and supersession-aware Waggle Recover.</p>}</section>
    <section className="panel"><h2>Live recovery feed</h2><table><thead><tr><th>Customer</th><th>Amount</th><th>Method / failure</th><th>Decision</th><th>Outcome</th><th></th></tr></thead><tbody>{recoveries.map((row, index) => <tr key={`${row.id}-${row.action ?? "pending"}-${row.outcome ?? "pending"}-${index}`}><td>{row.customer_id}</td><td>{money(row.amount)}</td><td>{row.method} · {row.failure_code}</td><td>{row.action ?? "—"}</td><td>{row.outcome ?? "—"} {row.recovered_amount ? money(row.recovered_amount) : ""}</td><td><button className="why" onClick={() => void inspectRecovery(row, true)}>Show graph</button></td></tr>)}{!recoveries.length && <tr><td colSpan={6}>No recoveries yet. Run a scenario to populate the feed.</td></tr>}</tbody></table></section>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
