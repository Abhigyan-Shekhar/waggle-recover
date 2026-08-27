import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
type Metrics = { gmv_at_risk: number; recovered_gmv: number; recovery_rate_pct: number; stale_evidence_prevented: number; policy_violations: number };
type Evidence = { rejection_reason?: string; metadata?: { instrument_id?: string; retry_after_seconds?: number } };
type Recovery = { id: string; customer_id: string; amount: number; method: string; instrument_id?: string; failure_code: string; action?: string; recommended_method?: string; retry_after_seconds?: number; outcome?: string; recovered_amount?: number; explanation?: string; discarded_json?: Evidence[] };
type SystemMetrics = { name: string; action_accuracy_pct: number; success_rate_pct: number; recovery_rate_gmv_pct: number; stale_rejection_rate_pct: number; avg_latency_ms: number };
type EvaluationSummary = { scenario_count: number; systems: Record<"baseline_a" | "baseline_b" | "system_c", SystemMetrics> };
type GraphNode = { id: string; label: string; node_type: string; tags?: string[]; valid_to?: string | null };
type GraphEdge = { source_id: string; target_id: string; relationship: string };
type MemoryGraph = { nodes: GraphNode[]; edges: GraphEdge[] };

const money = (paise = 0) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(paise / 100);

function GraphView({ graph }: { graph: MemoryGraph | null }) {
  if (!graph?.nodes.length) return <p className="muted">Select a completed recovery to reveal its evidence graph.</p>;
  const nodes = graph.nodes.slice(0, 10);
  const positions = new Map(nodes.map((node, index) => {
    const angle = (Math.PI * 2 * index) / nodes.length - Math.PI / 2;
    return [node.id, { x: 300 + Math.cos(angle) * 215, y: 180 + Math.sin(angle) * 125 }] as const;
  }));
  const edges = graph.edges.filter(edge => positions.has(edge.source_id) && positions.has(edge.target_id));

  return <div className="graph-wrap"><svg viewBox="0 0 600 360" role="img" aria-label="Decision memory graph">
    {edges.map((edge, index) => {
      const source = positions.get(edge.source_id)!;
      const target = positions.get(edge.target_id)!;
      return <g key={`${edge.source_id}-${edge.target_id}-${index}`}>
        <line className="edge" x1={source.x} y1={source.y} x2={target.x} y2={target.y} />
        <text className="edge-label" x={(source.x + target.x) / 2} y={(source.y + target.y) / 2}>{edge.relationship}</text>
      </g>;
    })}
    {nodes.map(node => {
      const position = positions.get(node.id)!;
      const stale = Boolean(node.valid_to) || node.tags?.some(tag => tag.includes("stale") || tag.includes("superseded"));
      return <g key={node.id} transform={`translate(${position.x} ${position.y})`}>
        <circle className={stale ? "node stale-node" : "node"} r="34" />
        <text className="node-label" textAnchor="middle" y="-2">{node.label.slice(0, 18)}</text>
        <text className="node-type" textAnchor="middle" y="13">{node.node_type}</text>
      </g>;
    })}
  </svg></div>;
}

function App() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [recoveries, setRecoveries] = useState<Recovery[]>([]);
  const [selected, setSelected] = useState<Recovery | null>(null);
  const [graph, setGraph] = useState<MemoryGraph | null>(null);
  const [evaluation, setEvaluation] = useState<EvaluationSummary | null>(null);
  const [scenarios, setScenarios] = useState<{ id: string; name: string }[]>([]);
  const [message, setMessage] = useState("Connect the simulator to see live decisions.");
  const [evaluationRunning, setEvaluationRunning] = useState(false);
  const evaluationRef = useRef<HTMLElement | null>(null);

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
    setMessage("Running deterministic scenario…");
    const response = await fetch(`${API}/api/simulator/scenario/${id}/run`, { method: "POST" });
    const body = await response.json();
    setMessage(response.ok ? `${body.scenario.name}: ${body.result.decision.action}` : body.detail ?? "Scenario failed");
    const latestRecoveries = await refresh();
    const current = latestRecoveries.find(row => row.id === body.result?.failure_id);
    if (current) await inspectRecovery(current);
  };

  const inspectRecovery = async (row: Recovery) => {
    setSelected(row);
    setGraph(null);
    const response = await fetch(`${API}/api/decisions/${row.id}/graph`);
    if (response.ok) setGraph(await response.json());
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
    <header><div><p className="eyebrow">Waggle Recover</p><h1>Revenue recovery, with a memory of what changed.</h1></div><button disabled={evaluationRunning} onClick={() => void runEvaluation()}>{evaluationRunning ? "Evaluating 200 cases…" : "Run evaluation"}</button></header>
    <p className="message">{message}</p>
    <section className="cards">{cards.map(([label, value]) => <article key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</section>
    <section className="grid">
      <article className="panel"><h2>Demo scenarios</h2><p>Every run uses the same normalized event pipeline as Razorpay webhooks.</p><div className="scenarios">{scenarios.map(scenario => <button key={scenario.id} onClick={() => void runScenario(scenario.id)}>{scenario.name}</button>)}</div></article>
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
          <div className="decision-step action-step"><span>3 · Final action</span><strong>{selected.action ?? "pending"}</strong><small>{selected.recommended_method ? `Use ${selected.recommended_method.toUpperCase()}` : selected.retry_after_seconds ? `Retry after ${selected.retry_after_seconds}s` : selected.outcome ?? "pending"}</small></div>
        </div>
        <details className="audit-details"><summary>View full decision explanation</summary><p className="full-explanation">{selected.explanation || "No explanation recorded."}</p></details>
        <details className="audit-details"><summary>View raw evidence audit ({rejectedEvidence.length} rejected)</summary><pre>{JSON.stringify(rejectedEvidence, null, 2)}</pre></details>
      </> : <p>Select a recovery in the feed to see the evidence audit trail.</p>}</article>
    </section>
    <section className="panel visual-grid"><div><h2>Decision memory graph</h2><p className="muted">Edges show which failures, instruments, outcomes, and policies informed the selected decision. Orange nodes are stale or superseded.</p></div><GraphView graph={graph} /></section>
    <section className="panel evaluation-panel" ref={evaluationRef}><div className="evaluation-heading"><div><p className="eyebrow">Proof at scale</p><h2>Three-system evaluation</h2></div>{evaluation && <span className="run-badge">{evaluation.scenario_count} seeded cases</span>}</div>{evaluation ? <><div className="evaluation-proof"><div><strong>{evaluation.systems.system_c.action_accuracy_pct}%</strong><span>parameter-aware action accuracy</span></div><div><strong>{evaluation.systems.system_c.stale_rejection_rate_pct}%</strong><span>exact stale-evidence rejection</span></div></div><p className="muted">Same cases, same outcome model. Only System C validates whether remembered evidence is still current.</p><div className="table-frame"><table><thead><tr><th>System</th><th>Action accuracy</th><th>Success rate</th><th>GMV recovery</th><th>Stale rejection</th><th>Avg latency</th></tr></thead><tbody>{systems.map(system => <tr key={system.name} className={system === evaluation.systems.system_c ? "system-c" : ""}><td>{system.name}</td><td>{system.action_accuracy_pct}%</td><td>{system.success_rate_pct}%</td><td>{system.recovery_rate_gmv_pct}%</td><td>{system.stale_rejection_rate_pct}%</td><td>{system.avg_latency_ms} ms</td></tr>)}</tbody></table></div></> : <p className="muted">Run the evaluation to compare blind retry, history-only recovery, and supersession-aware Waggle Recover.</p>}</section>
    <section className="panel"><h2>Live recovery feed</h2><table><thead><tr><th>Customer</th><th>Amount</th><th>Method / failure</th><th>Decision</th><th>Outcome</th><th></th></tr></thead><tbody>{recoveries.map(row => <tr key={row.id}><td>{row.customer_id}</td><td>{money(row.amount)}</td><td>{row.method} · {row.failure_code}</td><td>{row.action ?? "—"}</td><td>{row.outcome ?? "—"} {row.recovered_amount ? money(row.recovered_amount) : ""}</td><td><button className="why" onClick={() => void inspectRecovery(row)}>Why?</button></td></tr>)}{!recoveries.length && <tr><td colSpan={6}>No recoveries yet. Run a scenario to populate the feed.</td></tr>}</tbody></table></section>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
