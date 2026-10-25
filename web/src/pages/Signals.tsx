import { Fragment, lazy, Suspense, useEffect, useState } from "react";
import { api, Playbooks, SaveResult, SignalRow, SignalSpec } from "../api";
import { Notice, SaveFeedback, useLoad } from "../ui";
import OwnersTab from "./signals/OwnersTab";
import PlaybookTab from "./signals/PlaybookTab";

const SignalDialog = lazy(() => import("./signals/SignalDialog")); // only loaded when opened

const TABS = { signals: "Signals", playbook: "Playbook", owners: "Owners & notifications" } as const;
type Tab = keyof typeof TABS;
type Dialog = { initial: SignalSpec | null; originalName: string | null };

// A built-in duplicated as a starting point for a custom signal.
const copyOf = (s: SignalRow): SignalSpec => ({
  ...(s.spec ?? { rule: s.rule, specialist: s.specialist, thresholds: s.thresholds, direction: s.direction }),
  name: `${s.name}_custom`.slice(0, 48), description: s.description, how_calculated_note: s.how_calculated_note,
});

export default function SignalsPage() {
  const data = useLoad(() => api.get<{ signals: SignalRow[]; pack: string }>("/signals"));
  const playbooks = useLoad(() => api.get<Playbooks>("/playbooks"));
  const [tab, setTab] = useState<Tab>("signals");
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [book, setBook] = useState<Playbooks | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [error, setError] = useState("");

  useEffect(() => { if (data.data) setRows(data.data.signals); }, [data.data]);
  useEffect(() => { if (playbooks.data) setBook(playbooks.data); }, [playbooks.data]);

  const act = (p: Promise<SaveResult>) => { setError(""); p.then(setResult).catch((e: Error) => setError(e.message)); };
  const saveSignals = () => act(api.put<SaveResult>("/signals", { signals: rows }));
  const savePlaybook = () => book && act(api.put<SaveResult>("/playbooks", { owner_roles: book.owner_roles, plays: book.plays }));
  const remove = (name: string) => {
    if (window.confirm(`Delete the custom signal '${name}'? Plays that use it will stop matching it.`))
      act(api.del<SaveResult>(`/signals/custom/${encodeURIComponent(name)}`).then((r) => { data.reload(); playbooks.reload(); return r; }));
  };
  const saved = () => { setDialog(null); setResult({ ok: true, errors: [], warnings: [] }); data.reload(); playbooks.reload(); };
  const setRow = (i: number, patch: Partial<SignalRow>) => setRows(rows.map((r, j) => (i === j ? { ...r, ...patch } : r)));

  return (
    <>
      <h2>Signals & playbooks</h2>
      <p className="lead">Which risk signals the agent looks for and how each is calculated, the actions your company prefers, and who gets them.</p>
      <div className="tabs" role="tablist">
        {(Object.keys(TABS) as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => { setTab(t); setResult(null); }}>{TABS[t]}</button>
        ))}
      </div>
      <Notice kind="bad">{data.error || playbooks.error || error}</Notice>
      <SaveFeedback result={result} />

      {tab === "signals" && (
        <>
          <div className="row" style={{ marginBottom: 10 }}>
            <span className="muted">Built-in signals come with your industry pack; add your own for the warning signs only your company knows.</span>
            <span className="spacer" />
            <button className="primary" onClick={() => setDialog({ initial: null, originalName: null })}>+ New signal</button>
          </div>
          <div className="card" style={{ padding: 0 }}>
            <table>
              <thead><tr><th>On</th><th>Signal</th><th>Medium at</th><th>High at</th><th>Data</th><th /></tr></thead>
              <tbody>
                {rows.map((s, i) => (
                  <Fragment key={s.name}>
                    <tr>
                      <td><input type="checkbox" checked={s.enabled} onChange={(e) => setRow(i, { enabled: e.target.checked })} aria-label={`enable ${s.name}`} /></td>
                      <td>
                        <strong>{s.name}</strong> <span className={`badge ${s.origin === "custom" ? "ok" : ""}`}>{s.origin}</span> <span className="badge">{s.specialist}</span>
                        <div className="muted small">{s.description}</div>
                        <button className="link small" onClick={() => setOpen(open === s.name ? null : s.name)} aria-expanded={open === s.name}>How it's calculated {open === s.name ? "▴" : "▾"}</button>
                      </td>
                      {(["medium", "high"] as const).map((lvl) => (
                        <td key={lvl} style={{ width: 110 }}>
                          <input type="number" step="any" value={s.thresholds[lvl] ?? ""} placeholder="—" aria-label={`${s.name} ${lvl}`}
                            onChange={(e) => setRow(i, { thresholds: { ...s.thresholds, [lvl]: e.target.value === "" ? (undefined as unknown as number) : Number(e.target.value) } })} />
                        </td>
                      ))}
                      <td>{s.has_data ? <span className="badge ok">available</span> : <span className="badge warn" title={s.entities.join(", ")}>no source</span>}</td>
                      <td className="nowrap">
                        {s.origin === "custom" ? (
                          <>
                            <button onClick={() => setDialog({ initial: s.spec ?? null, originalName: s.name })}>Edit</button>{" "}
                            <button className="danger" onClick={() => remove(s.name)}>Delete</button>
                          </>
                        ) : <button onClick={() => setDialog({ initial: copyOf(s), originalName: null })} title="Start a custom signal from this one">Duplicate</button>}
                      </td>
                    </tr>
                    {open === s.name && (
                      <tr className="explain-row">
                        <td />
                        <td colSpan={5}>
                          <div className="explain">
                            <div>{s.explanation}</div>
                            {s.how_calculated_note && <div className="muted small">{s.how_calculated_note}</div>}
                            <div className="muted small">Data from: {s.entities.join(", ") || "—"}</div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          <p><button className="primary" onClick={saveSignals}>Save signals</button></p>
        </>
      )}

      {tab === "playbook" && book && <PlaybookTab data={book} onChange={(plays) => setBook({ ...book, plays })} onSave={savePlaybook} />}
      {tab === "owners" && book && <OwnersTab roles={book.owner_roles} onChange={(owner_roles) => setBook({ ...book, owner_roles })} onSave={savePlaybook} />}

      {dialog && (
        <Suspense fallback={null}>
          <SignalDialog initial={dialog.initial} originalName={dialog.originalName} onClose={() => setDialog(null)} onSaved={saved} />
        </Suspense>
      )}
    </>
  );
}
