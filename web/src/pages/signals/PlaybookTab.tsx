// The company's approved actions: when these signals fire, recommend this, owned by that role.
import { useState } from "react";
import { Play, Playbooks } from "../../api";
import { Field, Tip } from "../../ui";

const blank = (owner: string): Play => ({
  id: "", name: "", description: "", signals: [], min_severity: "any", action: "", steps: [], owner_role: owner,
  priority: "medium", due_in_days: 14, link: "", notify: true,
});

function DmPreview({ play }: { play: Play }) {
  return (
    <div className="dm">
      <div className="small muted">Slack DM to the {play.owner_role.toLowerCase()} · example account</div>
      <div><strong>Acme Corp</strong> — <span className="badge bad">high risk</span></div>
      <p><strong>{play.action || "(action)"}</strong><br /><span className="small">Priority {play.priority} · due in {play.due_in_days} days</span></p>
      {play.description && <p className="small">Why: {play.description}</p>}
      {play.steps.length > 0 && <ol className="small">{play.steps.map((s) => <li key={s}>{s}</li>)}</ol>}
      {play.link && <p className="small">Runbook: {play.link}</p>}
      <p className="small muted">Evidence: {play.signals.join(", ") || "(signals)"} — each with how it's calculated</p>
      <div className="row"><button>Done</button><button>Snooze</button><button>Not relevant</button></div>
    </div>
  );
}

interface Props { data: Playbooks; onChange: (plays: Play[]) => void; onSave: () => void }

export default function PlaybookTab({ data, onChange, onSave }: Props) {
  const [preview, setPreview] = useState<number | null>(null);
  const plays = data.plays;
  const set = (i: number, patch: Partial<Play>) => onChange(plays.map((p, j) => (i === j ? { ...p, ...patch } : p)));
  const toggleSignal = (i: number, name: string) =>
    set(i, { signals: plays[i].signals.includes(name) ? plays[i].signals.filter((s) => s !== name) : [...plays[i].signals, name] });

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <strong>Approved actions</strong>
        <Tip>When the chosen signals fire, the agent recommends your approved action (adapted to the account) and DMs its owner on Slack.</Tip>
      </div>
      {plays.map((p, i) => (
        <div key={i} className="card play">
          <div className="row">
            <div className="field" style={{ flex: 2 }}><label>Name</label><input value={p.name} onChange={(e) => set(i, { name: e.target.value })} aria-label="play name" /></div>
            <div className="field" style={{ flex: 1 }}><label>Only from severity</label>
              <select value={p.min_severity} onChange={(e) => set(i, { min_severity: e.target.value })} aria-label="minimum severity">
                <option value="any">any</option><option value="medium">medium</option><option value="high">high</option>
              </select></div>
          </div>
          <label>When any of these signals fire</label>
          <div className="chips">
            {data.signals.map((s) => (
              <label key={s} className={`chip ${p.signals.includes(s) ? "on" : ""}`}>
                <input type="checkbox" checked={p.signals.includes(s)} onChange={() => toggleSignal(i, s)} />{s}
              </label>
            ))}
          </div>
          <div className="field"><label>Recommended action</label><input value={p.action} onChange={(e) => set(i, { action: e.target.value })} aria-label="recommended action" /></div>
          <Field label="Why it works" tip="Shown to the owner with the action."><input value={p.description} onChange={(e) => set(i, { description: e.target.value })} aria-label="why it works" /></Field>
          <Field label="Steps" tip="One per line.">
            <textarea rows={3} value={p.steps.join("\n")} onChange={(e) => set(i, { steps: e.target.value.split("\n") })} aria-label="steps" /></Field>
          <div className="row">
            <div className="field" style={{ flex: 2 }}><label>Owner</label>
              <select value={p.owner_role} onChange={(e) => set(i, { owner_role: e.target.value })} aria-label="owner role">
                {[...new Set([p.owner_role, ...data.owner_roles.map((r) => r.name)])].map((r) => <option key={r}>{r}</option>)}
              </select></div>
            <div className="field" style={{ flex: 1 }}><label>Priority</label>
              <select value={p.priority} onChange={(e) => set(i, { priority: e.target.value })} aria-label="priority">
                <option>low</option><option>medium</option><option>high</option>
              </select></div>
            <div className="field" style={{ flex: 1 }}><label>Within (days)</label><input type="number" min={1} value={p.due_in_days} onChange={(e) => set(i, { due_in_days: Number(e.target.value) })} aria-label="due in days" /></div>
          </div>
          <Field label="Runbook link" tip="Optional: a page with how-to details, linked in the DM."><input value={p.link} placeholder="https://" onChange={(e) => set(i, { link: e.target.value })} aria-label="runbook link" /></Field>
          <div className="row">
            <label className="inline"><input type="checkbox" checked={p.notify} onChange={(e) => set(i, { notify: e.target.checked })} /> DM the owner on Slack</label>
            <span className="spacer" />
            <button onClick={() => setPreview(preview === i ? null : i)}>{preview === i ? "Hide preview" : "Preview Slack DM"}</button>
            <button className="danger" onClick={() => onChange(plays.filter((_, j) => j !== i))}>Remove</button>
          </div>
          {preview === i && <DmPreview play={{ ...p, steps: p.steps.filter((s) => s.trim()) }} />}
        </div>
      ))}
      <div className="row">
        <button onClick={() => onChange([...plays, blank(data.owner_roles[0]?.name ?? "Account owner")])}>+ Add play</button>
        <span className="spacer" />
        <button className="primary" onClick={onSave}>Save playbook</button>
      </div>
    </>
  );
}
