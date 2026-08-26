import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
type Metrics = { gmv_at_risk: number; recovered_gmv: number; recovery_rate_pct: number; stale_evidence_prevented: number; policy_violations: number };
type Recovery = { id: string; customer_id: string; amount: number; method: string; failure_code: string; action?: string; outcome?: string; recovered_amount?: number; explanation?: string; discarded_json?: unknown[] };
const money = (paise = 0) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(paise / 100);

function App() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [recoveries, setRecoveries] = useState<Recovery[]>([]);
  const [selected, setSelected] = useState<Recovery | null>(null);
  const [scenarios, setScenarios] = useState<{ id: string; name: string }[]>([]);
  const [message, setMessage] = useState("Connect the simulator to see live decisions.");
  const refresh = async () => {
    const [m, r, s] = await Promise.all([fetch(`${API}/api/payments/overview`), fetch(`${API}/api/payments/`), fetch(`${API}/api/simulator/scenarios/curated`)]);
    if (m.ok) setMetrics(await m.json());
    if (r.ok) setRecoveries((await r.json()).data ?? []);
    if (s.ok) setScenarios((await s.json()).scenarios ?? []);
  };
  useEffect(() => { void refresh(); }, []);
  const runScenario = async (id: string) => {
    setMessage("Running deterministic scenario…");
    const res = await fetch(`${API}/api/simulator/scenario/${id}/run`, { method: "POST" });
    const body = await res.json();
    setMessage(res.ok ? `${body.scenario.name}: ${body.result.decision.action}` : body.detail ?? "Scenario failed");
    await refresh();
  };
  const runEvaluation = async () => {
    setMessage("Evaluating 200 seeded synthetic histories…");
    const res = await fetch(`${API}/api/evaluation/run`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ seed: 42, count: 200 }) });
    const body = await res.json();
    setMessage(res.ok ? `Evaluation started — run ${body.run_id}. Metrics will be available when processing completes.` : "Evaluation failed");
  };
  const cards = [
    ["GMV at risk", money(metrics?.gmv_at_risk)], ["Recovered GMV", money(metrics?.recovered_gmv)], ["Recovery rate", `${metrics?.recovery_rate_pct ?? 0}%`], ["Stale evidence prevented", metrics?.stale_evidence_prevented ?? 0], ["Policy violations", metrics?.policy_violations ?? 0],
  ];
  return <main><header><div><p className="eyebrow">Waggle Recover</p><h1>Revenue recovery, with a memory of what changed.</h1></div><button onClick={() => void runEvaluation()}>Run evaluation</button></header>
    <p className="message">{message}</p><section className="cards">{cards.map(([label, value]) => <article key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</section>
    <section className="grid"><article className="panel"><h2>Demo scenarios</h2><p>Every run uses the same normalized event pipeline as Razorpay webhooks.</p><div className="scenarios">{scenarios.map(s => <button key={s.id} onClick={() => void runScenario(s.id)}>{s.name}</button>)}</div></article>
      <article className="panel"><h2>Decision inspector</h2>{selected ? <><p><b>Current failure:</b> {selected.failure_code} on {selected.method} for {money(selected.amount)}</p><p><b>Final action:</b> {selected.action ?? "pending"} · {selected.outcome ?? "pending"}</p><p>{selected.explanation || "No explanation recorded."}</p><div className="discarded"><b>Discarded / stale evidence</b><pre>{JSON.stringify(selected.discarded_json ?? [], null, 2)}</pre></div></> : <p>Select a recovery in the feed to see the evidence audit trail.</p>}</article></section>
    <section className="panel"><h2>Live recovery feed</h2><table><thead><tr><th>Customer</th><th>Amount</th><th>Method / failure</th><th>Decision</th><th>Outcome</th><th></th></tr></thead><tbody>{recoveries.map(row => <tr key={row.id}><td>{row.customer_id}</td><td>{money(row.amount)}</td><td>{row.method} · {row.failure_code}</td><td>{row.action ?? "—"}</td><td>{row.outcome ?? "—"} {row.recovered_amount ? money(row.recovered_amount) : ""}</td><td><button className="why" onClick={() => setSelected(row)}>Why?</button></td></tr>)}{!recoveries.length && <tr><td colSpan={6}>No recoveries yet. Run a scenario to populate the feed.</td></tr>}</tbody></table></section>
  </main>;
}
createRoot(document.getElementById("root")!).render(<App />);
