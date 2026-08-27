import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
type Metrics = { gmv_at_risk: number; recovered_gmv: number; recovery_rate_pct: number; stale_evidence_prevented: number; policy_violations: number };
type Recovery = { id: string; customer_id: string; amount: number; method: string; failure_code: string; action?: string; outcome?: string; recovered_amount?: number; explanation?: string; discarded_json?: unknown[] };
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

  const refresh = async () => {
    const [metricsResponse, recoveryResponse, scenarioResponse, runsResponse] = await Promise.all([
      fetch(`${API}/api/payments/overview`), fetch(`${API}/api/payments/`),
      fetch(`${API}/api/simulator/scenarios/curated`), fetch(`${API}/api/evaluation/runs`),
    ]);
    if (metricsResponse.ok) setMetrics(await metricsResponse.json());
    if (recoveryResponse.ok) setRecoveries((await recoveryResponse.json()).data ?? []);
    if (scenarioResponse.ok) setScenarios((await scenarioResponse.json()).scenarios ?? []);
    if (runsResponse.ok) {
      const completed = ((await runsResponse.json()).data ?? []).find((run: { status: string; summary?: EvaluationSummary }) => run.status === "completed" && run.summary);
      if (completed) setEvaluation(completed.summary);
    }
  };

  useEffect(() => { void refresh(); }, []);

  const runScenario = async (id: string) => {
    setMessage("Running deterministic scenario…");
    const response = await fetch(`${API}/api/simulator/scenario/${id}/run`, { method: "POST" });
    const body = await response.json();
    setMessage(response.ok ? `${body.scenario.name}: ${body.result.decision.action}` : body.detail ?? "Scenario failed");
    await refresh();
  };

  const inspectRecovery = async (row: Recovery) => {
    setSelected(row);
    setGraph(null);
    const response = await fetch(`${API}/api/decisions/${row.id}/graph`);
    if (response.ok) setGraph(await response.json());
  };

  const runEvaluation = async () => {
    setMessage("Evaluating 200 seeded synthetic histories…");
    const response = await fetch(`${API}/api/evaluation/run`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ seed: 42, count: 200 }) });
    const body = await response.json();
    if (!response.ok) { setMessage(body.detail ?? "Evaluation failed"); return; }
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 1000));
      const runResponse = await fetch(`${API}/api/evaluation/runs/${body.run_id}`);
      if (!runResponse.ok) continue;
      const run = await runResponse.json();
      if (run.status === "completed") {
        setEvaluation(run.summary);
        setMessage(`Evaluation complete — ${run.summary.scenario_count} scenarios, with every system decision persisted.`);
        return;
      }
      if (run.status === "failed") { setMessage(`Evaluation failed: ${run.summary?.error ?? "unknown error"}`); return; }
    }
    setMessage(`Evaluation ${body.run_id} is still running. Its results will appear after refresh.`);
  };

  const cards = [["GMV at risk", money(metrics?.gmv_at_risk)], ["Recovered GMV", money(metrics?.recovered_gmv)], ["Recovery rate", `${metrics?.recovery_rate_pct ?? 0}%`], ["Stale evidence prevented", metrics?.stale_evidence_prevented ?? 0], ["Policy violations", metrics?.policy_violations ?? 0]];
  const systems = evaluation ? [evaluation.systems.baseline_a, evaluation.systems.baseline_b, evaluation.systems.system_c] : [];

  return <main>
    <header><div><p className="eyebrow">Waggle Recover</p><h1>Revenue recovery, with a memory of what changed.</h1></div><button onClick={() => void runEvaluation()}>Run evaluation</button></header>
    <p className="message">{message}</p>
    <section className="cards">{cards.map(([label, value]) => <article key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</section>
    <section className="grid">
      <article className="panel"><h2>Demo scenarios</h2><p>Every run uses the same normalized event pipeline as Razorpay webhooks.</p><div className="scenarios">{scenarios.map(scenario => <button key={scenario.id} onClick={() => void runScenario(scenario.id)}>{scenario.name}</button>)}</div></article>
      <article className="panel"><h2>Decision inspector</h2>{selected ? <><p><b>Current failure:</b> {selected.failure_code} on {selected.method} for {money(selected.amount)}</p><p><b>Final action:</b> {selected.action ?? "pending"} · {selected.outcome ?? "pending"}</p><p>{selected.explanation || "No explanation recorded."}</p><div className="discarded"><b>Discarded / stale evidence</b><pre>{JSON.stringify(selected.discarded_json ?? [], null, 2)}</pre></div></> : <p>Select a recovery in the feed to see the evidence audit trail.</p>}</article>
    </section>
    <section className="panel visual-grid"><div><h2>Decision memory graph</h2><p className="muted">Edges show which failures, instruments, outcomes, and policies informed the selected decision. Orange nodes are stale or superseded.</p></div><GraphView graph={graph} /></section>
    <section className="panel"><h2>Three-system evaluation</h2>{evaluation ? <><p className="muted">Latest completed seeded run · {evaluation.scenario_count} scenarios</p><table><thead><tr><th>System</th><th>Action accuracy</th><th>Success rate</th><th>GMV recovery</th><th>Stale rejection</th><th>Avg latency</th></tr></thead><tbody>{systems.map(system => <tr key={system.name} className={system === evaluation.systems.system_c ? "system-c" : ""}><td>{system.name}</td><td>{system.action_accuracy_pct}%</td><td>{system.success_rate_pct}%</td><td>{system.recovery_rate_gmv_pct}%</td><td>{system.stale_rejection_rate_pct}%</td><td>{system.avg_latency_ms} ms</td></tr>)}</tbody></table></> : <p className="muted">Run the evaluation to compare blind retry, history-only recovery, and supersession-aware Waggle Recover.</p>}</section>
    <section className="panel"><h2>Live recovery feed</h2><table><thead><tr><th>Customer</th><th>Amount</th><th>Method / failure</th><th>Decision</th><th>Outcome</th><th></th></tr></thead><tbody>{recoveries.map(row => <tr key={row.id}><td>{row.customer_id}</td><td>{money(row.amount)}</td><td>{row.method} · {row.failure_code}</td><td>{row.action ?? "—"}</td><td>{row.outcome ?? "—"} {row.recovered_amount ? money(row.recovered_amount) : ""}</td><td><button className="why" onClick={() => void inspectRecovery(row)}>Why?</button></td></tr>)}{!recoveries.length && <tr><td colSpan={6}>No recoveries yet. Run a scenario to populate the feed.</td></tr>}</tbody></table></section>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
