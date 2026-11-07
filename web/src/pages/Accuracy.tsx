// "Was the agent right?" — renewals that already closed, compared with what the agent said
// at the time (live) or how they looked N days before they closed (replay). From the outcomes:
// suggested scoring and a suggested threshold per warning sign, applied by the admin.
import { useState } from "react";
import { api, Backtest, Rules, SaveResult, SignalRow, ThresholdSuggestion } from "../api";
import { label, Notice, PageHead, SaveFeedback, Tip, useLoad } from "../ui";

const daysAgo = (n: number) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
const pct = (x: number) => `${Math.round(x * 100)}%`;
const OUTCOME: Record<string, string> = { renewed: "ok", downgraded: "warn", churned: "bad" };
const BAND: Record<string, string> = { high: "bad", medium: "warn", low: "ok" };
const MODES = { auto: "Auto", live: "What it said then", replay: "Replay history" } as const;
type Mode = keyof typeof MODES;
const num = (v: number | null | undefined) => (v === null || v === undefined ? "—" : String(v));

function RulesText({ r }: { r: Rules }) {
  const factors = Object.entries(r.signal_factors ?? {});
  return (
    <span className="small">
      high {r.weights.high} · medium {r.weights.medium} · low {r.weights.low} points; medium risk from {r.thresholds.medium}, high from {r.thresholds.high}
      {factors.length > 0 && <> · per sign: {factors.map(([n, f]) => `${label(n)} ×${f}`).join(", ")}</>}
    </span>
  );
}

function ThresholdRow({ t, onApply }: { t: ThresholdSuggestion; onApply: (t: ThresholdSuggestion) => void }) {
  const sign = t.direction === "below" ? "≤" : "≥";
  const status = {
    not_enough: <span className="muted small">not enough history yet ({t.measured} measured, {t.losses} lost)</span>,
    no_signal: <span className="badge warn" title="No value of this sign separated lost renewals from renewed ones">does not predict losses</span>,
    same: <span className="badge ok">already right</span>,
    suggested: null,
  }[t.status];
  return (
    <tr>
      <td>{label(t.name)}</td>
      <td className="nowrap">{sign} {num(t.current.medium)} / {num(t.current.high)}</td>
      <td className="nowrap">{t.suggested ? <strong>{sign} {num(t.suggested.medium)} / {num(t.suggested.high)}</strong> : "—"}</td>
      <td className="small">
        {status ?? (t.now && t.then && (
          <>catches {t.then.caught} of {t.losses} losses, {t.then.false_alarms} false alarm(s)
            <span className="muted"> (now {t.now.caught} / {t.now.false_alarms})</span></>
        ))}
      </td>
      <td>{t.status === "suggested" && <button className="primary" onClick={() => onApply(t)} aria-label={`apply ${t.name}`}>Apply</button>}</td>
    </tr>
  );
}

export default function AccuracyPage() {
  const last = useLoad(() => api.get<Backtest | Record<string, never>>("/backtest"));
  const [form, setForm] = useState({ start: daysAgo(730), end: daysAgo(0), lead_days: 90, mode: "auto" as Mode });
  const [run, setRun] = useState<Backtest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<SaveResult | null>(null);
  const r = run ?? (last.data && "tested" in last.data ? (last.data as Backtest) : null);

  const go = () => {
    setBusy(true); setError("");
    api.post<Backtest>("/backtest", form).then(setRun).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  const apply = (rules: Rules) => api.put<SaveResult>("/rules", rules).then((x) => { setResult(x); go(); }).catch((e: Error) => setError(e.message));
  const applyThreshold = async (t: ThresholdSuggestion) => {
    try {
      const { signals } = await api.get<{ signals: SignalRow[] }>("/signals");
      const row = signals.find((s) => s.name === t.name);
      if (!row || !t.suggested) return;
      const thresholds: Record<string, number> = { ...row.thresholds, medium: t.suggested.medium };
      if (t.suggested.high === null) delete thresholds.high; else thresholds.high = t.suggested.high;
      const x = await api.put<SaveResult>("/signals", { signals: [{ name: t.name, enabled: row.enabled, thresholds }] });
      setResult({ ...x, note: `${label(t.name)}: thresholds updated.` });
      go();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const upload = (file: File | undefined) => {
    if (!file) return;
    const data = new FormData();
    data.append("file", file);
    api.post<SaveResult & { rows: number }>("/backtest/outcomes", data).then((x) => { setResult({ ...x, note: `${x.rows} outcome(s) loaded — run again to use them.` }); })
      .catch((e: Error) => setError(e.message));
  };
  const suggestions = (r?.thresholds ?? []).filter((t) => t.status !== "not_enough");
  const waiting = (r?.thresholds ?? []).filter((t) => t.status === "not_enough").length;

  return (
    <>
      <PageHead title="Accuracy" sub="Did the agent call it right? Its calls compared with what actually happened."
        help={<>Every time the agent checks renewals (Slack lists, the ask page, the nightly scan) its call is saved. Once those renewals close,
          <strong> What it said then</strong> compares the saved calls with the outcome. <strong>Replay history</strong> rebuilds each closed renewal as it
          looked N days before it closed — useful before there is any saved history. Free: no AI is used.</>} />
      <div className="card">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field"><label>Renewals closed from</label><input type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} aria-label="from" /></div>
          <div className="field"><label>to</label><input type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} aria-label="to" /></div>
          <div className="field"><label>Judge each one<Tip>Only calls made at least this long before the renewal closed count — early enough to act on.</Tip></label>
            <select value={form.lead_days} onChange={(e) => setForm({ ...form, lead_days: Number(e.target.value) })} aria-label="lead days">
              {[30, 60, 90, 120, 180].map((d) => <option key={d} value={d}>{d} days before it closed</option>)}
            </select></div>
          <div className="field"><label>Using<Tip>Auto uses the saved calls when there are any, otherwise replays history.</Tip></label>
            <div className="segmented" role="radiogroup" aria-label="mode">
              {(Object.keys(MODES) as Mode[]).map((m) => (
                <button key={m} role="radio" aria-checked={form.mode === m} className={form.mode === m ? "on" : ""} onClick={() => setForm({ ...form, mode: m })}>{MODES[m]}</button>
              ))}
            </div></div>
          <button className="primary" style={{ marginBottom: 4 }} onClick={go} disabled={busy}>{busy ? "Running…" : "Run backtest"}</button>
        </div>
        <Notice kind="bad">{error || last.error}</Notice>
        <SaveFeedback result={result} />
      </div>

      {r && r.mode === "replay" && r.live_available === 0 && (
        <Notice>No saved calls old enough for these renewals yet, so history was replayed. Saved calls are used automatically once renewals checked today close.</Notice>
      )}
      {r && r.tested === 0 && <Notice kind="warn">No closed renewals with a known outcome in this period. Widen the period, or upload outcomes below.</Notice>}
      {r && r.tested > 0 && (
        <>
          <div className="card headline">
            <div className="row">
              <div className="big">Caught <strong>{r.metrics.caught} of {r.at_risk}</strong> renewals that were lost or shrank, {r.lead_days} days before they closed</div>
              <span className="spacer" />
              <span className="badge">{r.mode === "live" ? "what it said then" : "replayed"}</span>
            </div>
            <div className="muted">{r.metrics.false_alarms} false alarm(s) among {r.tested - r.at_risk} that renewed in full · a "biggest deals first" list of the same length would have caught {r.baseline_caught}</div>
            <div className="tiles">
              <div><span>{pct(r.metrics.hit_rate)}</span>of losses caught</div>
              <div><span>{pct(r.metrics.precision)}</span>of flags were right</div>
              <div><span>{r.tested}</span>renewals tested</div>
            </div>
          </div>

          <div className="grid2">
            <div className="card">
              <div className="row"><h3>Does "high risk" mean high risk?</h3>
                <Tip>For each risk level the agent gave, how many of those renewals were then lost or shrank. High should be lost far more often than Low.</Tip></div>
              <table>
                <thead><tr><th>Called</th><th>Renewals</th><th>Lost or shrank</th></tr></thead>
                <tbody>
                  {r.calibration.map((c) => (
                    <tr key={c.band}>
                      <td><span className={`badge ${BAND[c.band]}`}>{c.band}</span></td>
                      <td>{c.renewals}</td>
                      <td>{c.renewals ? <>{c.lost} <span className="muted">({pct(c.share ?? 0)})</span></> : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card">
              <div className="row"><h3>Scoring</h3>
                <Tip>Points per warning sign by severity, the score cut-offs, and a factor per sign learned from how often it came before a loss. Within one data section the strongest sign counts in full and the rest at half, so one problem is not counted four times.</Tip></div>
              <p>Now: <RulesText r={r.rules} /></p>
              {r.suggested ? (
                <div className="notice ok">
                  Suggested: <RulesText r={r.suggested} /> — would catch {r.suggested.metrics.caught} of {r.at_risk} with {r.suggested.metrics.false_alarms} false alarm(s).{" "}
                  <button className="primary" onClick={() => apply(r.suggested!)}>Apply</button>
                </div>
              ) : <p className="muted small">The current scoring is already the best of the settings tried on these renewals.</p>}
            </div>
          </div>

          <div className="card" style={{ padding: 0 }}>
            <div className="row" style={{ padding: "16px 20px 4px" }}>
              <h3>Thresholds per warning sign</h3>
              <Tip wide>From the value each sign measured on every closed renewal — fired or not. Medium is the cut that best separates lost from renewed;
                high is where flags were lost at least twice as often as average. Shown as medium / high. Needs at least 8 renewals with 3 lost and 3 renewed; nothing changes until you click Apply.</Tip>
              <span className="spacer" />
              {waiting > 0 && <span className="muted small">{waiting} sign(s) waiting for more history</span>}
            </div>
            <table>
              <thead><tr><th>Warning sign</th><th>Now</th><th>Suggested</th><th>Effect on these renewals</th><th /></tr></thead>
              <tbody>
                {suggestions.map((t) => <ThresholdRow key={t.name} t={t} onApply={applyThreshold} />)}
                {suggestions.length === 0 && <tr><td colSpan={5} className="muted">Not enough closed renewals yet to suggest thresholds.</td></tr>}
              </tbody>
            </table>
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
                    <td title={`as of ${c.as_of}${c.band_then ? `; said ${c.band_then} then` : ""}`}>
                      {!c.enough ? <span className="badge">not enough data</span>
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
                      {s.weak && <span className="badge warn" title="Consider turning it off or raising its threshold">wrong more often than chance</span>}{" "}
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
        <div className="row"><h3>Outcomes</h3>
          <Tip wide>By default the outcome comes from the renewal's stage in your CRM ("Closed Lost" → churned; won but smaller than before → downgraded).
            Upload a CSV with <code>account_id, close_date, outcome</code> (renewed, downgraded or churned) to add or correct them.</Tip></div>
        <input type="file" accept=".csv" onChange={(e) => upload(e.target.files?.[0])} aria-label="outcomes file" style={{ width: "auto" }} />
        {r && r.unknown_outcome.length > 0 && (
          <p className="small">Outcome unknown for {r.unknown_outcome.length}: {r.unknown_outcome.slice(0, 8).map((u) => `${u.name} (${u.close_date}, stage "${u.stage ?? "?"}")`).join("; ")}</p>
        )}
      </div>
    </>
  );
}
