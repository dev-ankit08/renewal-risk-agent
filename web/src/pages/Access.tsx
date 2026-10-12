import { useEffect, useState } from "react";
import { api, SaveResult } from "../api";
import { Notice, SaveFeedback, useLoad } from "../ui";

type Scope = "all" | Record<string, string | string[]> | undefined;
type Entry = { email?: string; domain?: string; role: string; scope?: Scope };

// access.yaml allows a single value (`owner_email: self`) or a list (`region: [EMEA, UK]`).
const scopeText = (s: Scope) =>
  s === "all" ? "all" : s ? Object.entries(s).map(([k, v]) => `${k}=${(Array.isArray(v) ? v : [v]).join(",")}`).join("; ") : "";
function parseScope(text: string): Scope {
  const t = text.trim();
  if (!t) return undefined;
  if (t.toLowerCase() === "all") return "all";
  const out: Record<string, string[]> = {};
  for (const part of t.split(";")) {
    const [k, v] = part.split("=");
    if (k?.trim() && v) out[k.trim()] = v.split(",").map((x) => x.trim()).filter(Boolean);
  }
  return Object.keys(out).length ? out : undefined;
}

export default function AccessPage() {
  const access = useLoad(() => api.get<{ users: Entry[]; domains: Entry[]; roles: string[]; api_keys: { name: string; as: string }[] }>("/access"));
  const slack = useLoad(() => api.get<{ command: string; reply_visibility: string; alerts: Record<string, unknown>; bot_token_set: boolean; app_token_set: boolean }>("/slack"));
  const [users, setUsers] = useState<(Entry & { scopeText: string })[]>([]);
  const [domains, setDomains] = useState<(Entry & { scopeText: string })[]>([]);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [newKey, setNewKey] = useState<{ name: string; as: string; key?: string }>({ name: "", as: "" });
  const [slackForm, setSlackForm] = useState({ bot_token: "", app_token: "", command: "/renewal-risk", reply_visibility: "ephemeral", channel: "", dm_owner: true, dm_roles: "director", min_band: "high" });
  const [testChannel, setTestChannel] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!access.data) return;
    setUsers(access.data.users.map((u) => ({ ...u, scopeText: scopeText(u.scope) })));
    setDomains(access.data.domains.map((d) => ({ ...d, scopeText: scopeText(d.scope) })));
  }, [access.data]);
  useEffect(() => {
    if (!slack.data) return;
    const a = slack.data.alerts as { channel?: string; dm_owner?: boolean; dm_roles?: string[]; min_band?: string };
    setSlackForm((f) => ({ ...f, command: slack.data!.command, reply_visibility: slack.data!.reply_visibility, channel: a.channel ?? "",
      dm_owner: a.dm_owner ?? true, dm_roles: (a.dm_roles ?? ["director"]).join(","), min_band: a.min_band ?? "high" }));
  }, [slack.data]);

  const roles = access.data?.roles ?? ["admin", "director", "manager", "csm", "viewer"];
  const saveAccess = () =>
    api.put<SaveResult>("/access", {
      users: users.map((u) => ({ email: u.email, role: u.role, scope: parseScope(u.scopeText) })),
      domains: domains.map((d) => ({ domain: d.domain, role: d.role, scope: parseScope(d.scopeText) })),
    }).then((r) => { setResult(r); access.reload(); }).catch((e: Error) => setError(e.message));
  const createKey = () =>
    api.post<SaveResult & { key: string }>("/access/api-keys", { name: newKey.name, as_email: newKey.as })
      .then((r) => { setNewKey({ ...newKey, key: r.key }); access.reload(); }).catch((e: Error) => setError(e.message));
  const saveSlack = () =>
    api.put<SaveResult>("/slack", {
      bot_token: slackForm.bot_token || null, app_token: slackForm.app_token || null, command: slackForm.command, reply_visibility: slackForm.reply_visibility,
      alerts: { channel: slackForm.channel, dm_owner: slackForm.dm_owner, dm_roles: slackForm.dm_roles.split(",").map((s) => s.trim()).filter(Boolean), min_band: slackForm.min_band },
    }).then((r) => { setResult(r); slack.reload(); }).catch((e: Error) => setError(e.message));

  const table = (list: (Entry & { scopeText: string })[], set: (l: (Entry & { scopeText: string })[]) => void, key: "email" | "domain") => (
    <table>
      <thead><tr><th>{key === "email" ? "Email (as in Slack)" : "Email domain"}</th><th>Role</th><th>Can see <span className="muted">(e.g. region=EMEA,UK; portfolio=Banking · all · empty = own accounts)</span></th><th /></tr></thead>
      <tbody>
        {list.map((u, i) => (
          <tr key={i}>
            <td><input value={u[key] ?? ""} onChange={(e) => set(list.map((x, j) => (i === j ? { ...x, [key]: e.target.value } : x)))} /></td>
            <td><select value={u.role} onChange={(e) => set(list.map((x, j) => (i === j ? { ...x, role: e.target.value } : x)))}>{roles.map((r) => <option key={r}>{r}</option>)}</select></td>
            <td><input value={u.scopeText} onChange={(e) => set(list.map((x, j) => (i === j ? { ...x, scopeText: e.target.value } : x)))} /></td>
            <td><button className="danger" onClick={() => set(list.filter((_, j) => j !== i))}>✕</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <>
      <h2>Access & Slack</h2>
      <p className="lead">Who can ask about which accounts — in Slack, the API and alerts. People not listed get nothing.</p>
      <Notice kind="bad">{access.error || slack.error || error}</Notice>
      <SaveFeedback result={result} />
      <div className="card">
        <h3>People</h3>
        {table(users, setUsers, "email")}
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={() => setUsers([...users, { email: "", role: "director", scopeText: "" }])}>+ Add person</button>
        </div>
        <h3 style={{ marginTop: 16 }}>Everyone on a domain</h3>
        {table(domains, setDomains, "domain")}
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={() => setDomains([...domains, { domain: "", role: "viewer", scopeText: "" }])}>+ Add domain</button>
          <span className="spacer" />
          <button className="primary" onClick={saveAccess}>Save access</button>
        </div>
      </div>

      <div className="card">
        <h3>API keys</h3>
        <table><tbody>
          {access.data?.api_keys.map((k) => (
            <tr key={k.name}><td>{k.name}</td><td className="muted">acts as {k.as}</td>
              <td><button className="danger" onClick={() => api.del(`/access/api-keys/${k.name}`).then(() => access.reload())}>Revoke</button></td></tr>
          ))}
        </tbody></table>
        <div className="row" style={{ marginTop: 8 }}>
          <input style={{ width: 180 }} placeholder="name, e.g. dashboard" value={newKey.name} onChange={(e) => setNewKey({ name: e.target.value, as: newKey.as })} />
          <input style={{ width: 240 }} placeholder="acts as (email above)" value={newKey.as} onChange={(e) => setNewKey({ name: newKey.name, as: e.target.value })} />
          <button onClick={createKey} disabled={!newKey.name || !newKey.as}>Create key</button>
        </div>
        {newKey.key && <Notice kind="ok">New key (copy it now — it is not shown again): <code>{newKey.key}</code></Notice>}
      </div>

      <div className="card">
        <h3>Slack</h3>
        <p className="muted">Create the app from <code>slack_manifest.yaml</code> (api.slack.com/apps → From a manifest), install it, and paste its tokens.
          {slack.data && <> Bot token {slack.data.bot_token_set ? <span className="badge ok">set</span> : <span className="badge warn">missing</span>} · App token {slack.data.app_token_set ? <span className="badge ok">set</span> : <span className="badge warn">missing</span>}</>}
        </p>
        <div className="grid2">
          <div><label>Bot token (xoxb-…)</label><input type="password" placeholder="leave empty to keep" value={slackForm.bot_token} onChange={(e) => setSlackForm({ ...slackForm, bot_token: e.target.value })} /></div>
          <div><label>App token (xapp-…, Socket Mode)</label><input type="password" placeholder="leave empty to keep" value={slackForm.app_token} onChange={(e) => setSlackForm({ ...slackForm, app_token: e.target.value })} /></div>
          <div><label>Slash command</label><input value={slackForm.command} onChange={(e) => setSlackForm({ ...slackForm, command: e.target.value })} /></div>
          <div><label>Slash-command answers are</label>
            <select value={slackForm.reply_visibility} onChange={(e) => setSlackForm({ ...slackForm, reply_visibility: e.target.value })}>
              <option value="ephemeral">visible only to the asker</option><option value="in_channel">posted in the channel</option>
            </select></div>
          <div><label>Alert channel</label><input placeholder="#renewal-risk" value={slackForm.channel} onChange={(e) => setSlackForm({ ...slackForm, channel: e.target.value })} /></div>
          <div><label>Alert when an account rises to</label>
            <select value={slackForm.min_band} onChange={(e) => setSlackForm({ ...slackForm, min_band: e.target.value })}><option value="high">high risk</option><option value="medium">medium risk or above</option></select></div>
          <div><label>Also DM these roles (if the account is in their scope)</label><input value={slackForm.dm_roles} onChange={(e) => setSlackForm({ ...slackForm, dm_roles: e.target.value })} /></div>
          <div><label style={{ fontWeight: 400 }}><input type="checkbox" checked={slackForm.dm_owner} onChange={(e) => setSlackForm({ ...slackForm, dm_owner: e.target.checked })} /> DM the account owner</label></div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <input style={{ width: 200 }} placeholder="#channel for a test" value={testChannel} onChange={(e) => setTestChannel(e.target.value)} />
          <button disabled={!testChannel} onClick={() => api.post("/slack/test", { channel: testChannel }).then(() => setResult({ ok: true, errors: [], warnings: [], note: "Test message sent." })).catch((e: Error) => setError(e.message))}>Send test message</button>
          <span className="spacer" />
          <button className="primary" onClick={saveSlack}>Save Slack settings</button>
        </div>
      </div>
    </>
  );
}
