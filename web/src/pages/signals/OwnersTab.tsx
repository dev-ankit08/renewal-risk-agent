// Who owns each action (and how to find that person per account), the DM
// settings, and what happened to the actions that were sent.
import { useEffect, useState } from "react";
import { ActionItem, ActionSettings, ActionSummary, api, OwnerRole, SaveResult } from "../../api";
import { Notice, SaveFeedback, useLoad } from "../../ui";

type How = "account_field" | "access_role" | "emails" | "";
const HOW: Record<How, string> = { account_field: "a field on the account", access_role: "people with this access role", emails: "these email addresses", "": "nobody (label only)" };

const howOf = (r: OwnerRole): How => (Object.keys(r.find)[0] as How) ?? "";
const valueOf = (r: OwnerRole) => { const v = Object.values(r.find)[0]; return Array.isArray(v) ? v.join(", ") : String(v ?? ""); };
const find = (how: How, value: string): OwnerRole["find"] =>
  how === "" ? {} : how === "emails" ? { emails: value.split(",").map((e) => e.trim()) } /* blanks kept while typing; the server drops them */ : { [how]: value.trim() };

interface Props { roles: OwnerRole[]; onChange: (roles: OwnerRole[]) => void; onSave: () => void }

export default function OwnersTab({ roles, onChange, onSave }: Props) {
  const slack = useLoad(() => api.get<{ actions: ActionSettings; bot_token_set: boolean }>("/slack"));
  const actions = useLoad(() => api.get<{ actions: ActionItem[]; summary: ActionSummary }>("/actions"));
  const [settings, setSettings] = useState<ActionSettings | null>(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  useEffect(() => { if (slack.data) setSettings(slack.data.actions); }, [slack.data]);

  const set = (i: number, patch: Partial<OwnerRole>) => onChange(roles.map((r, j) => (i === j ? { ...r, ...patch } : r)));
  const saveSettings = () => settings && api.put<SaveResult>("/slack/actions", settings).then(setResult);
  const s = actions.data?.summary;

  return (
    <>
      <div className="card">
        <h3>Owner roles</h3>
        <p className="muted">How the agent finds the person to DM for each account. People only ever hear about accounts they are allowed to see.</p>
        {roles.map((r, i) => (
          <div className="row cond" key={i}>
            <input style={{ flex: 2 }} value={r.name} onChange={(e) => set(i, { name: e.target.value })} aria-label="role name" />
            <select value={howOf(r)} onChange={(e) => set(i, { find: find(e.target.value as How, valueOf(r)) })} aria-label="find by">
              {Object.entries(HOW).map(([k, text]) => <option key={k} value={k}>{text}</option>)}
            </select>
            {howOf(r) && <input style={{ flex: 2 }} value={valueOf(r)} aria-label="find value"
              placeholder={howOf(r) === "account_field" ? "owner_email" : howOf(r) === "access_role" ? "director" : "a@company.com, b@company.com"}
              onChange={(e) => set(i, { find: find(howOf(r), e.target.value) })} />}
            <button onClick={() => onChange(roles.filter((_, j) => j !== i))} aria-label="remove role">✕</button>
          </div>
        ))}
        <div className="row">
          <button onClick={() => onChange([...roles, { name: "", find: { account_field: "owner_email" } }])}>+ Add role</button>
          <span className="spacer" />
          <button className="primary" onClick={onSave}>Save owner roles</button>
        </div>
      </div>

      <div className="card">
        <h3>Action DMs</h3>
        <Notice kind="bad">{slack.error}</Notice>
        {slack.data && !slack.data.bot_token_set && <Notice kind="warn">Slack is not connected yet (Access & Slack) — actions are tracked but no DM is sent.</Notice>}
        <SaveFeedback result={result} />
        {settings && (
          <>
            <label className="inline"><input type="checkbox" checked={settings.dm_owners} onChange={(e) => setSettings({ ...settings, dm_owners: e.target.checked })} /> DM each recommended action to its owner</label>
            <div className="row">
              <div className="field"><label>When the account is</label>
                <select value={settings.min_band} onChange={(e) => setSettings({ ...settings, min_band: e.target.value })} aria-label="minimum band">
                  <option value="high">high risk</option><option value="medium">medium risk or above</option>
                </select></div>
              <div className="field"><label>Snooze for (days)</label><input type="number" min={1} max={90} value={settings.snooze_days} onChange={(e) => setSettings({ ...settings, snooze_days: Number(e.target.value) })} aria-label="snooze days" /></div>
              <div className="field"><label>Don't repeat a closed action for (days)</label><input type="number" min={1} max={365} value={settings.cooldown_days} onChange={(e) => setSettings({ ...settings, cooldown_days: Number(e.target.value) })} aria-label="cooldown days" /></div>
            </div>
            <button className="primary" onClick={saveSettings}>Save DM settings</button>
          </>
        )}
      </div>

      <div className="card">
        <h3>Actions sent (last 30 days)</h3>
        {s && <p className="small">{s.total} sent · {s.open} open · {s.done} done · {s.snoozed} snoozed · {s.not_relevant} not relevant · <strong>{s.overdue} overdue</strong>
          {s.completion_rate !== null && <> · {Math.round(s.completion_rate * 100)}% completed</>}</p>}
        {actions.data && actions.data.actions.length === 0 && <p className="muted">No actions yet — they appear after an assessment at or above the band above.</p>}
        {actions.data && actions.data.actions.length > 0 && (
          <table>
            <thead><tr><th>Account</th><th>Action</th><th>Owner</th><th>Status</th><th>Due</th></tr></thead>
            <tbody>
              {actions.data.actions.map((a) => (
                <tr key={a.id}>
                  <td>{a.account_name || a.account_id}</td>
                  <td className="small">{a.action}</td>
                  <td className="small">{a.owner_email}</td>
                  <td><span className={`badge ${a.status === "done" ? "ok" : a.status === "open" && a.due_at * 1000 < Date.now() ? "bad" : ""}`}>{a.status.replace("_", " ")}</span></td>
                  <td className="small">{a.due_at ? new Date(a.due_at * 1000).toLocaleDateString() : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
