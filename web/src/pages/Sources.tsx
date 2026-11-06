import { useState } from "react";
import { api, AuthMethod, Catalog, Category, SaveResult, Section, SourceInfo, Status, SystemOption } from "../api";
import Checklist from "../Checklist";
import { go, label, Modal, Notice, SaveFeedback, useLoad } from "../ui";
import DataTypeRow from "./sources/DataTypeRow";
import SectionConnect from "./sources/SectionConnect";
import SectionModal from "./sources/SectionModal";

export default function SourcesPage({ params, status, onChange }: { params: URLSearchParams; status: Status; onChange: () => void }) {
  const catalog = useLoad(() => api.get<Catalog>("/catalog"));
  const sources = useLoad(() => api.get<{ sources: SourceInfo[]; primary: string }>("/sources"));
  const [connecting, setConnecting] = useState(false);
  const [restFor, setRestFor] = useState<Category | null>(null);
  const [editing, setEditing] = useState<Section | null | "new">(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [tests, setTests] = useState<Record<string, { ok: boolean; detail: string } | "running">>({});

  const reload = () => {
    catalog.reload();
    sources.reload();
  };
  const saved = (r: SaveResult) => { setResult(r); reload(); onChange(); }; // the sidebar depends on what is connected
  const fail = (e: Error) => setResult({ ok: false, errors: [e.message], warnings: [] });
  const test = (name: string) => {
    setTests((t) => ({ ...t, [name]: "running" }));
    api.post<{ ok: boolean; detail: string }>(`/sources/${name}/test`).then((r) => setTests((t) => ({ ...t, [name]: r })));
  };
  const connectOAuth = (name: string) =>
    api.post<{ authorize_url: string }>(`/sources/${name}/oauth/start`).then((r) => (window.location.href = r.authorize_url)).catch(fail);
  const remove = (name: string) => {
    if (!confirm(`Remove the system "${name}" and its stored credentials? Data types read from it will need a new object.`)) return;
    api.del<SaveResult>(`/sources/${name}`).then(saved).catch(fail);
  };
  const upload = (name: string, file: File | undefined) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    api.post(`/sources/${name}/upload`, form).then(() => saved({ ok: true, errors: [], warnings: [], note: `${file.name} uploaded to ${name}.` })).catch(fail);
  };
  const removeSection = (c: Category) => {
    if (!confirm(`Delete the section "${c.label}"? Its connections and specialist are removed; the data in your systems is untouched.`)) return;
    api.del<SaveResult>(`/sections/${c.key}`).then(saved).catch(fail);
  };
  const changePack = (pack: string) => api.put<SaveResult>("/pack", { pack }).then(saved);

  const cat = catalog.data;
  const connected = cat?.connected ?? [];

  return (
    <>
      <div className="row">
        <div>
          <h2>Data sources</h2>
          <p className="lead">Connect each system once, then choose for every kind of data which object it is read from.</p>
        </div>
        <span className="spacer" />
        <button onClick={() => setEditing("new")}>+ Add section</button>
        {cat && (
          <label style={{ margin: 0 }}>
            Business type{" "}
            <select value={cat.pack.name} onChange={(e) => changePack(e.target.value)} style={{ width: "auto" }}>
              {cat.packs.map((p) => <option key={p.name} value={p.name}>{p.label}</option>)}
            </select>
          </label>
        )}
      </div>
      {params.get("connected") && <Notice kind="ok">Connected {params.get("connected")}. Now pick its objects in the sections below.</Notice>}
      {params.get("oauth_error") && <Notice kind="bad">Sign-in failed: {params.get("oauth_error")}</Notice>}
      <Checklist />
      <SaveFeedback result={result} />
      {(catalog.error || sources.error) && <Notice kind="bad">{catalog.error || sources.error}</Notice>}

      <div className="card">
        <div className="row">
          <h3>Connected systems</h3>
          <span className="spacer" />
          <button className="primary" onClick={() => setConnecting(true)}>+ Connect a system</button>
        </div>
        <p className="muted">Sign in once; every section below can then read any of these systems.</p>
        {(sources.data?.sources ?? []).length === 0 && <p className="muted">Nothing connected yet.</p>}
        {(sources.data?.sources ?? []).map((s) => {
          const t = tests[s.name] ?? s.last_test;
          const needsOAuth = s.auth_method === "oauth2" && !s.connection.oauth_connected;
          return (
            <div key={s.name} className="row dtrow">
              <strong>{s.name}</strong>
              <span className="muted">{connected.find((c) => c.name === s.name)?.label ?? s.preset ?? s.type}</span>
              {s.primary && <span className="badge">customer list</span>}
              {!s.connection.credentials_complete && <span className="badge bad">missing {s.connection.missing.join(", ")}</span>}
              {needsOAuth && <span className="badge warn">sign-in needed</span>}
              {t === "running" && <span className="badge">testing…</span>}
              {t && t !== "running" && <span className={`badge ${t.ok ? "ok" : "bad"}`} title={t.detail}>{t.ok ? "test OK" : "test failed"}</span>}
              <span className="spacer" />
              {s.type === "csv" && (
                <label className="inline">Upload a file <input type="file" accept=".csv,.xlsx" onChange={(e) => upload(s.name, e.target.files?.[0])} style={{ width: "auto" }} aria-label={`upload to ${s.name}`} /></label>
              )}
              {s.auth_method === "oauth2" && <button className={needsOAuth ? "primary" : ""} onClick={() => connectOAuth(s.name)}>{needsOAuth ? "Sign in" : "Reconnect"}</button>}
              <button onClick={() => test(s.name)}>Test</button>
              <button className="danger" onClick={() => remove(s.name)}>Remove</button>
              {t && t !== "running" && !t.ok && <div className="notice bad small" style={{ width: "100%" }}>{t.detail}</div>}
            </div>
          );
        })}
      </div>

      {cat?.categories.map((c) => (
        <div className="card" key={c.key}>
          <div className="row">
            <h3>{c.label}</h3>
            {c.custom && <span className="badge ok">custom section</span>}
            {c.required && <span className="badge">required</span>}
            {c.rows.every((r) => r.locked) ? <span className="badge ok">objects locked</span>
              : c.rows.some((r) => r.source) ? <span className="badge">using defaults</span> : <span className="badge warn">not connected</span>}
            <span className="spacer" />
            {c.custom && c.section && (
              <>
                <button onClick={() => setEditing(c.section!)}>Edit</button>
                <button onClick={() => go(`signals?new=${c.entities[0]}`)}>+ Add warning sign</button>
                <button onClick={() => setRestFor(c)}>+ REST / MCP</button>
                <button className="danger" onClick={() => removeSection(c)}>Delete</button>
              </>
            )}
          </div>
          <p className="muted">{c.description}</p>
          {c.section && (
            <p className="small">
              <span className="muted">Data type</span> <strong>{label(c.section.data_type.name)}</strong>{" "}
              <span className="muted">({c.section.data_type.fields.map((f) => label(f.name)).join(", ")})</span>
              <br /><span className="muted">Its specialist looks for:</span> {c.section.focus}
            </p>
          )}
          {c.rows.map((r) => <DataTypeRow key={`${r.entity}:${r.source}:${r.object}:${r.locked}`} row={r} connected={connected} onSaved={saved} />)}
        </div>
      ))}

      {editing && (
        <SectionModal section={editing === "new" ? null : editing} salesforce={cat?.salesforce_sources ?? []}
          onClose={() => setEditing(null)} onSaved={(r) => { setEditing(null); saved(r); }} />
      )}
      {restFor && <SectionConnect category={restFor} onClose={() => setRestFor(null)} onSaved={(r, name) => { setRestFor(null); saved(r); test(name); }} />}
      {connecting && cat && (
        <ConnectModal
          systems={cat.systems}
          existing={(sources.data?.sources ?? []).map((s) => s.name)}
          callbackUrl={status.callback_url}
          onClose={() => setConnecting(false)}
          onSaved={(r, name, oauth) => {
            setConnecting(false);
            saved(r);
            if (oauth) connectOAuth(name);
            else test(name);
          }}
        />
      )}
    </>
  );
}

// Connect one system: a ready-made one (sign in), a database (connection string) or CSV / Excel files.
function ConnectModal(props: {
  systems: SystemOption[]; existing: string[]; callbackUrl: string;
  onClose: () => void; onSaved: (r: SaveResult, name: string, oauth: boolean) => void;
}) {
  const [choice, setChoice] = useState<string>(props.systems[0]?.preset ?? "csv");
  const system = props.systems.find((s) => s.preset === choice);
  const [name, setName] = useState(choice);
  const [method, setMethod] = useState<string>(system?.auth_methods[0]?.method ?? "");
  const [values, setValues] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  const [testData, setTestData] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const auth: AuthMethod | undefined = system?.auth_methods.find((m) => m.method === method) ?? system?.auth_methods[0];

  const pick = (value: string) => {
    setChoice(value);
    setName(value === "sql" ? "database" : value === "csv" ? "files" : value);
    setMethod(props.systems.find((x) => x.preset === value)?.auth_methods[0]?.method ?? "");
    setValues({});
  };

  const save = async () => {
    if (props.existing.includes(name) && !confirm(`A system named "${name}" exists. Replace its settings?`)) return;
    setBusy(true); setError(null);
    try {
      if (system && auth) {
        const params: Record<string, string> = {};
        const secrets: Record<string, string> = {};
        for (const p of auth.params) {
          const v = values[p.name] ?? (p.default ?? "");
          if (v) (p.secret ? secrets : params)[p.name] = v;
        }
        props.onSaved(await api.put<SaveResult>(`/sources/${name}`, { preset: system.preset, auth_method: auth.method, params, secrets, test_data: testData }), name, auth.oauth);
      } else {
        const r = await api.put<SaveResult>(`/sources/${name}`, { type: choice, secrets: { dsn: values.dsn ?? "" }, test_data: testData });
        if (choice === "csv" && file) {
          const form = new FormData();
          form.append("file", file);
          await api.post(`/sources/${name}/upload`, form);
        }
        props.onSaved(r, name, false);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Connect a system" onClose={props.onClose}>
      <label>System</label>
      <select value={choice} onChange={(e) => pick(e.target.value)} aria-label="system">
        {props.systems.map((s) => <option key={s.preset} value={s.preset}>{s.label}</option>)}
        <option value="sql">Database (SQL)</option>
        <option value="csv">CSV / Excel files</option>
      </select>
      {system && <p className="muted small">{system.description}</p>}
      <label>Name <span className="hint">— a short id, e.g. crm, billing, support</span></label>
      <input value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} aria-label="system name" />
      {system && system.auth_methods.length > 1 && (
        <>
          <label>How to sign in</label>
          {system.auth_methods.map((m) => (
            <div key={m.method}>
              <label style={{ fontWeight: 400, display: "inline" }}>
                <input type="radio" checked={auth?.method === m.method} onChange={() => setMethod(m.method)} /> {m.label}
              </label>
            </div>
          ))}
        </>
      )}
      {auth?.help && <pre>{auth.help}</pre>}
      {auth?.oauth && <p className="small">Callback URL to register: <code>{props.callbackUrl}</code></p>}
      {auth?.params.map((p) => (
        <div key={p.name}>
          <label>{p.label}{p.required && " *"} {p.description && <span className="hint">— {p.description}</span>}</label>
          <input type={p.secret ? "password" : "text"} autoComplete="off" value={values[p.name] ?? ""}
            placeholder={p.secret ? "stored encrypted; never written to config files" : p.default ?? ""}
            onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} />
        </div>
      ))}
      {choice === "sql" && (
        <>
          <label>Connection string (DSN) <span className="hint">— e.g. postgresql+psycopg://reader:…@host/db; stored encrypted</span></label>
          <input type="password" autoComplete="off" value={values.dsn ?? ""} onChange={(e) => setValues({ ...values, dsn: e.target.value })} aria-label="dsn" />
          <p className="muted small">Read-only: only SELECT queries are ever run. Its tables appear as objects in each section.</p>
        </>
      )}
      {choice === "csv" && (
        <>
          <label>First file <span className="hint">— more can be uploaded later; each file appears as an object</span></label>
          <input type="file" accept=".csv,.xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="first file" />
        </>
      )}
      <label style={{ fontWeight: 400 }}>
        <input type="checkbox" checked={testData} onChange={(e) => setTestData(e.target.checked)} /> This is a sandbox with synthetic test data only
      </label>
      <Notice kind="bad">{error}</Notice>
      <div className="row" style={{ marginTop: 12 }}>
        <span className="spacer" />
        <button onClick={props.onClose}>Cancel</button>
        <button className="primary" disabled={busy || !name} onClick={save}>{auth?.oauth ? "Save & sign in" : "Save & test"}</button>
      </div>
    </Modal>
  );
}
