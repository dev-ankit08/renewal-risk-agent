import { useState } from "react";
import { ActionSummary, api } from "../api";
import { Notice, useLoad } from "../ui";

interface HealthData {
  errors: string[];
  warnings: string[];
  sources: Record<string, { last_test: { ok: boolean; detail: string } | null; credentials_complete: boolean; oauth_connected?: boolean }>;
  linking: { accounts: number; sources: Record<string, { coverage: number; review: unknown[] }> } | null;
  daily: { day: string; assessments: number; cost_usd: number; high: number }[];
  recent: { id: number; account_id: string; account_name: string; score: number; band: string; cost_usd: number; created_at: number }[];
  model_configured: boolean;
  actions: ActionSummary;
}

export default function HealthPage() {
  const health = useLoad(() => api.get<HealthData>("/health"));
  const feedback = useLoad(() => api.get<{ feedback: { account_id: string; verdict: string; outcome: string; user_email: string; created_at: number; note: string }[] }>("/feedback"));
  const [sent, setSent] = useState<Record<number, string>>({});

  const give = (row: HealthData["recent"][number], patch: { verdict?: string; outcome?: string }) =>
    api.post("/feedback", { account_id: row.account_id, assessment_id: row.id, ...patch }).then(() => {
      setSent({ ...sent, [row.id]: patch.verdict ?? patch.outcome ?? "" });
      feedback.reload();
    });

  const h = health.data;
  const totalCost = h?.daily.reduce((s, d) => s + (d.cost_usd || 0), 0) ?? 0;
  return (
    <>
      <h2>Health & feedback</h2>
      <p className="lead">Is everything connected, what is it costing, and is the agent right?</p>
      <Notice kind="bad">{health.error}</Notice>
      {h && (
        <>
          {h.errors.length === 0 ? <Notice kind="ok">Configuration is complete.</Notice> : (
            <Notice kind="warn">To finish setup:<ul>{h.errors.map((e) => <li key={e}>{e}</li>)}</ul></Notice>
          )}
          {h.warnings.length > 0 && <details className="card"><summary>{h.warnings.length} note(s)</summary><ul>{h.warnings.map((w) => <li key={w} className="small">{w}</li>)}</ul></details>}
          <div className="grid2">
            <div className="card">
              <h3>Sources</h3>
              <table><tbody>
                {Object.entries(h.sources).map(([name, s]) => (
                  <tr key={name}>
                    <td>{name}</td>
                    <td>
                      {!s.credentials_complete && <span className="badge bad">credentials missing</span>}{" "}
                      {s.oauth_connected === false && <span className="badge warn">sign-in needed</span>}{" "}
                      {s.last_test ? <span className={`badge ${s.last_test.ok ? "ok" : "bad"}`} title={s.last_test.detail}>{s.last_test.ok ? "test OK" : "test failed"}</span> : <span className="badge">not tested</span>}
                    </td>
                  </tr>
                ))}
              </tbody></table>
            </div>
            <div className="card">
              <h3>Account matching</h3>
              {h.linking ? Object.entries(h.linking.sources).map(([name, s]) => (
                <div key={name} className="small" style={{ marginBottom: 8 }}>
                  {name}: {Math.round(s.coverage * 100)}% matched{s.review.length ? `, ${s.review.length} to review` : ""}
                  <div className="meter"><span style={{ width: `${Math.round(s.coverage * 100)}%` }} /></div>
                </div>
              )) : <p className="muted small">Not built yet — open Account matching.</p>}
            </div>
            <div className="card">
              <h3>Last 30 days</h3>
              <p>{h.daily.reduce((s, d) => s + d.assessments, 0)} assessments · {h.daily.reduce((s, d) => s + (d.high || 0), 0)} high risk · ${totalCost.toFixed(2)} model cost</p>
              {!h.model_configured && <Notice kind="warn">ANTHROPIC_API_KEY is not set — full assessments and AI mapping are unavailable.</Notice>}
            </div>
            <div className="card">
              <h3>Actions (last 30 days)</h3>
              <p>{h.actions.open} open · {h.actions.done} done · <strong>{h.actions.overdue} overdue</strong>
                {h.actions.completion_rate !== null && <> · {Math.round(h.actions.completion_rate * 100)}% completed</>}</p>
              <p className="muted small">{h.actions.total} recommended actions sent to their owners. Details: Signals & playbooks › Owners & notifications.</p>
            </div>
          </div>
          <div className="card">
            <h3>Recent assessments — was the agent right?</h3>
            <p className="muted small">Your verdicts and the real outcomes are kept with each answer. To measure accuracy on renewals that already closed, open Accuracy.</p>
            <table>
              <thead><tr><th>Account</th><th>Risk</th><th>When</th><th>Your verdict</th><th>Actual outcome</th></tr></thead>
              <tbody>
                {h.recent.map((r) => (
                  <tr key={r.id}>
                    <td>{r.account_name || r.account_id}</td>
                    <td><span className={`badge ${r.band === "high" ? "bad" : r.band === "medium" ? "warn" : "ok"}`}>{r.score} · {r.band}</span></td>
                    <td className="small muted">{new Date(r.created_at * 1000).toLocaleString()}</td>
                    <td className="row">
                      {sent[r.id] ? <span className="badge ok">{sent[r.id]}</span> : (
                        <><button onClick={() => give(r, { verdict: "agree" })}>Agree</button><button onClick={() => give(r, { verdict: "disagree" })}>Disagree</button></>
                      )}
                    </td>
                    <td>
                      <select defaultValue="" onChange={(e) => e.target.value && give(r, { outcome: e.target.value })}>
                        <option value="">—</option><option value="renewed">renewed</option><option value="downgraded">downgraded</option><option value="churned">churned</option>
                      </select>
                    </td>
                  </tr>
                ))}
                {h.recent.length === 0 && <tr><td colSpan={5} className="muted">No assessments yet — ask in Slack or on the Ask page.</td></tr>}
              </tbody>
            </table>
          </div>
          {feedback.data && feedback.data.feedback.length > 0 && (
            <details className="card">
              <summary>{feedback.data.feedback.length} feedback entr{feedback.data.feedback.length === 1 ? "y" : "ies"}</summary>
              <table><tbody>
                {feedback.data.feedback.map((f, i) => (
                  <tr key={i}><td>{f.account_id}</td><td>{f.verdict ?? ""}</td><td>{f.outcome ?? ""}</td><td className="muted small">{f.user_email}</td></tr>
                ))}
              </tbody></table>
            </details>
          )}
        </>
      )}
    </>
  );
}
