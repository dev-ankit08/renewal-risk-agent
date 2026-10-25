// "How often is it right?" — the backtest: replay closed renewals as they looked
// N days before they closed, compare with what happened, tune the scoring.
import { useState } from "react";
import { api, Backtest, Rules, SaveResult } from "../api";
import { label, Notice, SaveFeedback, useLoad } from "../ui";

const daysAgo = (n: number) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
const pct = (x: number) => `${Math.round(x * 100)}%`;
const OUTCOME: Record<string, string> = { renewed: "ok", downgraded: "warn", churned: "bad" };

function RulesText({ r }: { r: Rules }) {
  return <span className="small">high {r.weights.high} · medium {r.weights.medium} · low {r.weights.low} points; medium risk from {r.thresholds.medium}, high from {r.thresholds.high}</span>;
}

export default function AccuracyPage() {
  const last = useLoad(() => api.get<Backtest | Record<string, never>>("/backtest"));
  const [form, setForm] = useState({ start: daysAgo(730), end: daysAgo(0), lead_days: 90 });
  const [run, setRun] = useState<Backtest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<SaveResult | null>(null);
  const r = run ?? (last.data && "tested" in last.data ? (last.data as Backtest) : null);

  const go = () => {
    setBusy(true); setError("");
    api.post<Backtest>("/backtest", form).then(setRun).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  const apply = (rules: Rules) => api.put<SaveResult>("/rules", rules).then((x) => { setResult(x); go(); });
  const upload = (file: File | undefined) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    api.post<SaveResult & { rows: number }>("/backtest/outcomes", form).then((x) => { setResult({ ...x, note: `${x.rows} outcome(s) loaded — run again to use them.` }); })
      .catch((e: Error) => setError(e.message));
  };

  return (
    <>
      <h2>Accuracy</h2>
      <p className="lead">How often is the agent right? Each renewal that already closed is replayed as it looked before it closed — only the
        data that existed then — and the agent's call is compared with what actually happened. Free: no AI is used.</p>
      <div className="card">
        <div className="row">
          <div className="field"><label>Renewals closed from</label><input type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} aria-label="from" /></div>
          <div className="field"><label>to</label><input type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} aria-label="to" /></div>
          <div className="field"><label>Judge each one</label>
            <select value={form.lead_days} onChange={(e) => setForm({ ...form, lead_days: Number(e.target.value) })} aria-label="lead days">
              {[30, 60, 90, 120, 180].map((d) => <option key={d} value={d}>{d} days before it closed</option>)}
            </select></div>
          <button className="primary" style={{ alignSelf: "flex-end", marginBottom: 6 }} onClick={go} disabled={busy}>{busy ? "Running…" : "Run backtest"}</button>
        </div>
        <Notice kind="bad">{error || last.error}</Notice>
        <SaveFeedback result={result} />
      </div>

      {r && r.tested === 0 && <Notice kind="warn">No closed renewals with a known outcome in this period. Widen the period, or upload outcomes below.</Notice>}
      {r && r.tested > 0 && (
        <>
          <div className="card headline">
            <div className="big">Caught <strong>{r.metrics.caught} of {r.at_risk}</strong> renewals that were lost or shrank, {r.lead_days} days before they closed</div>
            <div className="muted">{r.metrics.false_alarms} false alarm(s) among {r.tested - r.at_risk} that renewed in full · a "biggest deals first" list of the same length would have caught {r.baseline_caught}</div>
            <div className="tiles">
              <div><span>{pct(r.metrics.hit_rate)}</span>of losses caught</div>
              <div><span>{pct(r.metrics.precision)}</span>of flags were right</div>
              <div><span>{r.tested}</span>renewals tested</div>
            </div>
          </div>

          <div className="card">
            <h3>Scoring</h3>
            <p>Current: <RulesText r={r.rules} /></p>
            {r.suggested ? (
              <div className="notice ok">
                Suggested: <RulesText r={r.suggested} /> — would catch {r.suggested.metrics.caught} of {r.at_risk} with {r.suggested.metrics.false_alarms} false alarm(s).{" "}
                <button className="primary" onClick={() => apply(r.suggested!)}>Apply</button>
              </div>
            ) : <p className="muted small">The current scoring is already the best of the settings tried on these renewals.</p>}
          </div>

          <div className="card" style={{ padding: 0 }}>
            <table>
              <thead><tr><th>Account</th><th>Closed</th><th>What happened</th><th>Agent's call then</th><th>Warning signs then</th></tr></thead>
              <tbody>
                {r.cases.map((c) => (
                  <tr key={c.account_id + c.close_date}>
                    <td>{c.name}</td>
                    <td className="small">{c.close_date}</td>
                    <td><span className={`badge ${OUTCOME[c.outcome]}`}>{c.outcome}</span></td>
                    <td>{!c.enough ? <span className="badge" title={`${c.checked} of ${c.total} signs had data`}>not enough data</span>
                      : c.predicted ? <span className="badge bad">at risk · {c.score}</span> : <span className="badge">quiet · {c.score}</span>}</td>
                    <td className="small">{c.fired.map((s) => label(s.name)).join(", ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card" style={{ padding: 0 }}>
            <table>
              <thead><tr><th>Warning sign</th><th>Fired</th><th>Before a loss</th><th>Right when it fired</th><th /></tr></thead>
              <tbody>
                {r.signals.map((s) => (
                  <tr key={s.name}>
                    <td>{label(s.name)}</td><td>{s.fired}</td><td>{s.preceded_loss}</td>
                    <td>{pct(s.precision)}{s.lift !== null && <span className="muted small"> ({s.lift}× the average)</span>}</td>
                    <td className="small">
                      {s.weak && <span className="badge warn">wrong more often than chance — consider turning off or raising its threshold</span>}{" "}
                      {s.approximate && <span className="badge" title="Reads a field that only holds today's value (status, stage, open/closed…)">approximate</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="card">
        <h3>Outcomes</h3>
        <p className="muted small">By default the outcome comes from the renewal's stage in your CRM ("Closed Lost" → churned; won but
          smaller than before → downgraded). Upload a CSV with <code>account_id, close_date, outcome</code> (renewed, downgraded or churned) to add or correct them.</p>
        <input type="file" accept=".csv" onChange={(e) => upload(e.target.files?.[0])} aria-label="outcomes file" style={{ width: "auto" }} />
        {r && r.unknown_outcome.length > 0 && (
          <p className="small">Outcome unknown for {r.unknown_outcome.length}: {r.unknown_outcome.slice(0, 8).map((u) => `${u.name} (${u.close_date}, stage "${u.stage ?? "?"}")`).join("; ")}</p>
        )}
      </div>
    </>
  );
}
