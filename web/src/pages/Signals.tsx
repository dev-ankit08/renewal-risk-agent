import { useEffect, useState } from "react";
import { api, SaveResult, SignalRow } from "../api";
import { Notice, SaveFeedback, useLoad } from "../ui";

type Play = { signals: string[]; action: string; owner_role: string; due_in_days: number };

export default function SignalsPage() {
  const data = useLoad(() => api.get<{ signals: SignalRow[]; pack: string }>("/signals"));
  const playbooks = useLoad(() => api.get<{ owner_roles: string[]; plays: Play[] }>("/playbooks"));
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [plays, setPlays] = useState<Play[]>([]);
  const [roles, setRoles] = useState("");
  const [result, setResult] = useState<SaveResult | null>(null);

  useEffect(() => { if (data.data) setRows(data.data.signals); }, [data.data]);
  useEffect(() => {
    if (playbooks.data) { setPlays(playbooks.data.plays ?? []); setRoles((playbooks.data.owner_roles ?? []).join(", ")); }
  }, [playbooks.data]);

  const saveSignals = () => api.put<SaveResult>("/signals", { signals: rows }).then(setResult);
  const savePlays = () =>
    api.put<SaveResult>("/playbooks", { owner_roles: roles.split(",").map((r) => r.trim()).filter(Boolean), plays }).then(setResult);
  const setRow = (i: number, patch: Partial<SignalRow>) => setRows(rows.map((r, j) => (i === j ? { ...r, ...patch } : r)));
  const setPlay = (i: number, patch: Partial<Play>) => setPlays(plays.map((p, j) => (i === j ? { ...p, ...patch } : p)));

  return (
    <>
      <h2>Signals & playbooks</h2>
      <p className="lead">Which risk signals the agent looks for, at what thresholds — and the actions your company prefers.</p>
      <Notice kind="bad">{data.error || playbooks.error}</Notice>
      <SaveFeedback result={result} />
      <div className="card" style={{ padding: 0 }}>
        <table>
          <thead><tr><th>On</th><th>Signal</th><th>Medium at</th><th>High at</th><th>Data</th></tr></thead>
          <tbody>
            {rows.map((s, i) => (
              <tr key={s.name}>
                <td><input type="checkbox" checked={s.enabled} onChange={(e) => setRow(i, { enabled: e.target.checked })} aria-label={`enable ${s.name}`} /></td>
                <td><strong>{s.name}</strong> <span className="badge">{s.specialist}</span><div className="muted small">{s.description}</div></td>
                {(["medium", "high"] as const).map((lvl) => (
                  <td key={lvl} style={{ width: 110 }}>
                    <input type="number" step="any" value={s.thresholds[lvl] ?? ""} placeholder="—"
                      onChange={(e) => setRow(i, { thresholds: { ...s.thresholds, [lvl]: e.target.value === "" ? (undefined as unknown as number) : Number(e.target.value) } })} />
                  </td>
                ))}
                <td>{s.has_data ? <span className="badge ok">available</span> : <span className="badge warn" title={s.entities.join(", ")}>no source</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p><button className="primary" onClick={saveSignals}>Save signals</button></p>

      <div className="card">
        <h3>Playbook</h3>
        <p className="muted">When these signals fire, the agent recommends your approved action (adapted to the account).</p>
        <label>Owner roles <span className="hint">— comma separated</span></label>
        <input value={roles} onChange={(e) => setRoles(e.target.value)} />
        {plays.map((p, i) => (
          <div key={i} className="card" style={{ marginTop: 10 }}>
            <label>When any of these signals fire <span className="hint">— comma separated names</span></label>
            <input value={p.signals.join(", ")} onChange={(e) => setPlay(i, { signals: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
            <label>Recommended action</label>
            <input value={p.action} onChange={(e) => setPlay(i, { action: e.target.value })} />
            <div className="row">
              <div style={{ flex: 2 }}><label>Owner</label><input value={p.owner_role} onChange={(e) => setPlay(i, { owner_role: e.target.value })} /></div>
              <div style={{ flex: 1 }}><label>Within (days)</label><input type="number" value={p.due_in_days} onChange={(e) => setPlay(i, { due_in_days: Number(e.target.value) })} /></div>
              <button className="danger" style={{ alignSelf: "flex-end" }} onClick={() => setPlays(plays.filter((_, j) => j !== i))}>Remove</button>
            </div>
          </div>
        ))}
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={() => setPlays([...plays, { signals: [], action: "", owner_role: "Account owner", due_in_days: 14 }])}>+ Add play</button>
          <span className="spacer" />
          <button className="primary" onClick={savePlays}>Save playbook</button>
        </div>
      </div>
    </>
  );
}
