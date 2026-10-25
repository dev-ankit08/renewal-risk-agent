import { useState } from "react";
import { api, AuthMethod, Catalog, Category, SaveResult, Section, SourceInfo, Status, SystemOption } from "../api";
import Checklist from "../Checklist";
import { go, label, Modal, Notice, SaveFeedback, useLoad } from "../ui";
import CsvModal from "./sources/CsvModal";
import SectionConnect from "./sources/SectionConnect";
import SectionModal from "./sources/SectionModal";

export default function SourcesPage({ params, status, onPackChange }: { params: URLSearchParams; status: Status; onPackChange: () => void }) {
  const catalog = useLoad(() => api.get<Catalog>("/catalog"));
  const sources = useLoad(() => api.get<{ sources: SourceInfo[]; primary: string }>("/sources"));
  const [adding, setAdding] = useState<Category | null>(null);
  const [editing, setEditing] = useState<Section | null | "new">(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [tests, setTests] = useState<Record<string, { ok: boolean; detail: string } | "running">>({});

  const reload = () => {
    catalog.reload();
    sources.reload();
  };
  const test = (name: string) => {
    setTests((t) => ({ ...t, [name]: "running" }));
    api.post<{ ok: boolean; detail: string }>(`/sources/${name}/test`).then((r) => setTests((t) => ({ ...t, [name]: r })));
  };
  const connectOAuth = (name: string) =>
    api.post<{ authorize_url: string }>(`/sources/${name}/oauth/start`).then((r) => (window.location.href = r.authorize_url))
      .catch((e: Error) => setResult({ ok: false, errors: [e.message], warnings: [] }));
  const remove = (name: string) => {
    if (!confirm(`Remove the source "${name}" and its stored credentials?`)) return;
    api.del<SaveResult>(`/sources/${name}`).then((r) => { setResult(r); reload(); });
  };
  const removeSection = (c: Category) => {
    if (!confirm(`Delete the section "${c.label}"? Its connections and specialist are removed; the data in your systems is untouched.`)) return;
    api.del<SaveResult>(`/sections/${c.key}`).then((r) => { setResult(r); reload(); })
      .catch((e: Error) => setResult({ ok: false, errors: [e.message], warnings: [] }));
  };
  const changePack = (pack: string) =>
    api.put<SaveResult>("/pack", { pack }).then((r) => { setResult(r); onPackChange(); reload(); });

  const byName = Object.fromEntries((sources.data?.sources ?? []).map((s) => [s.name, s]));
  const cat = catalog.data;

  return (
    <>
      <div className="row">
        <div>
          <h2>Data sources</h2>
          <p className="lead">Pick where each kind of data lives, sign in, and test the connection.</p>
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
      {params.get("connected") && <Notice kind="ok">Connected {params.get("connected")}. Test it, then map its fields.</Notice>}
      {params.get("oauth_error") && <Notice kind="bad">Sign-in failed: {params.get("oauth_error")}</Notice>}
      <Checklist />
      <SaveFeedback result={result} />
      {(catalog.error || sources.error) && <Notice kind="bad">{catalog.error || sources.error}</Notice>}

      {cat?.categories.map((c) => (
        <div className="card" key={c.key}>
          <div className="row">
            <h3>{c.label}</h3>
            {c.custom && <span className="badge ok">custom section</span>}
            {c.required && <span className="badge">required</span>}
            {c.configured_sources.length > 0 ? <span className="badge ok">connected</span> : <span className="badge warn">not connected</span>}
            <span className="spacer" />
            {c.custom && c.section && (
              <>
                <button onClick={() => setEditing(c.section!)}>Edit</button>
                <button onClick={() => go(`signals?new=${c.entities[0]}&specialist=${c.key}`)}>+ Add warning sign</button>
                <button className="danger" onClick={() => removeSection(c)}>Delete</button>
              </>
            )}
            <button onClick={() => setAdding(c)}>+ Connect a system</button>
          </div>
          <p className="muted">{c.description}</p>
          {c.section && (
            <p className="small">
              <span className="muted">Data type</span> <strong>{label(c.section.data_type.name)}</strong>{" "}
              <span className="muted">({c.section.data_type.fields.map((f) => label(f.name)).join(", ")})</span>
              <br /><span className="muted">Its specialist looks for:</span> {c.section.focus}
            </p>
          )}
          {c.configured_sources.map((name) => {
            const s = byName[name];
            if (!s) return null;
            const t = tests[name] ?? s.last_test;
            const needsOAuth = s.auth_method === "oauth2" && !s.connection.oauth_connected;
            return (
              <div key={name} className="row" style={{ borderTop: "1px solid var(--line)", paddingTop: 10, marginTop: 8 }}>
                <strong>{name}</strong>
                <span className="muted">{s.preset ?? s.type}</span>
                {s.primary && <span className="badge">primary account list</span>}
                {!s.connection.credentials_complete && <span className="badge bad">missing {s.connection.missing.join(", ")}</span>}
                {needsOAuth && <span className="badge warn">sign-in needed</span>}
                {t === "running" && <span className="badge">testing…</span>}
                {t && t !== "running" && <span className={`badge ${t.ok ? "ok" : "bad"}`} title={t.detail}>{t.ok ? "test OK" : "test failed"}</span>}
                <span className="spacer" />
                {s.auth_method === "oauth2" && <button className={needsOAuth ? "primary" : ""} onClick={() => connectOAuth(name)}>{needsOAuth ? "Sign in" : "Reconnect"}</button>}
                <button onClick={() => test(name)}>Test</button>
                <button onClick={() => go(`mapping/${name}`)}>Map fields</button>
                {!s.primary && <button onClick={() => api.put<SaveResult>("/primary", { source: name }).then((r) => { setResult(r); reload(); })}>Make primary</button>}
                <button className="danger" onClick={() => remove(name)}>Remove</button>
                {t && t !== "running" && !t.ok && <div className="notice bad small" style={{ width: "100%" }}>{t.detail}</div>}
              </div>
            );
          })}
        </div>
      ))}

      {editing && (
        <SectionModal section={editing === "new" ? null : editing} salesforce={cat?.salesforce_sources ?? []}
          onClose={() => setEditing(null)} onSaved={(r) => { setEditing(null); setResult(r); reload(); }} />
      )}
      {adding?.custom && (
        <SectionConnect category={adding} salesforce={cat?.salesforce_sources ?? []} onClose={() => setAdding(null)} onSaved={(r, name) => { setAdding(null); setResult(r); reload(); if (name) test(name); }} />
      )}
      {adding && !adding.custom && cat && (
        <ConnectModal
          category={adding}
          catalog={cat}
          existing={Object.keys(byName)}
          callbackUrl={status.callback_url}
          onClose={() => setAdding(null)}
          onSaved={(r, name, oauth) => {
            setAdding(null);
            setResult(r);
            reload();
            if (oauth) connectOAuth(name);
            else if (name) test(name);
          }}
        />
      )}
    </>
  );
}

function ConnectModal(props: {
  category: Category; catalog: Catalog; existing: string[]; callbackUrl: string;
  onClose: () => void; onSaved: (r: SaveResult, name: string, oauth: boolean) => void;
}) {
  const { category } = props;
  const [choice, setChoice] = useState<string>(category.systems[0]?.preset ?? "csv");
  const system: SystemOption | undefined = category.systems.find((s) => s.preset === choice);
  const [name, setName] = useState(category.key);
  const [method, setMethod] = useState<string>(system?.auth_methods[0]?.method ?? "");
  const [values, setValues] = useState<Record<string, string>>({});
  const [also, setAlso] = useState<Record<string, boolean>>({});
  const [testData, setTestData] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const auth: AuthMethod | undefined = system?.auth_methods.find((m) => m.method === method) ?? system?.auth_methods[0];

  const pick = (preset: string) => {
    setChoice(preset);
    const s = category.systems.find((x) => x.preset === preset);
    setMethod(s?.auth_methods[0]?.method ?? "");
    setValues({});
    setAlso({});
  };

  const picker = (
    <>
      <label>System</label>
      <select value={choice} onChange={(e) => pick(e.target.value)}>
        {category.systems.map((s) => <option key={s.preset} value={s.preset}>{s.label}</option>)}
        <option value="csv">CSV / Excel upload</option>
      </select>
    </>
  );
  if (choice === "csv") return <CsvModal category={category} picker={picker} name={name} setName={setName} onClose={props.onClose} onSaved={props.onSaved} />;

  const otherEntities = system ? allPresetEntities(props.catalog, system.preset).filter((e) => !category.entities.includes(e)) : [];
  const save = () => {
    if (!system || !auth) return;
    if (props.existing.includes(name) && !confirm(`A source named "${name}" exists. Replace its settings?`)) return;
    const params: Record<string, string> = {};
    const secrets: Record<string, string> = {};
    for (const p of auth.params) {
      const v = values[p.name] ?? (p.default ?? "");
      if (v) (p.secret ? secrets : params)[p.name] = v;
    }
    const disabled = otherEntities.filter((e) => also[e] === false);
    setBusy(true);
    api.put<SaveResult>(`/sources/${name}`, { preset: system.preset, auth_method: auth.method, params, secrets, disabled_entities: disabled, test_data: testData })
      .then((r) => props.onSaved(r, name, auth.oauth))
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <Modal title={`Connect: ${category.label}`} onClose={props.onClose}>
      {picker}
      {system && <p className="muted small">{system.description}</p>}
      <label>Source name <span className="hint">— a short id, e.g. crm, billing, support</span></label>
      <input value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} />
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
          <label>
            {p.label}
            {p.required && " *"} {p.description && <span className="hint">— {p.description}</span>}
          </label>
          <input
            type={p.secret ? "password" : "text"}
            autoComplete="off"
            placeholder={p.secret ? "stored encrypted; never written to config files" : p.default ?? ""}
            value={values[p.name] ?? ""}
            onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
          />
        </div>
      ))}
      {otherEntities.length > 0 && (
        <>
          <label>{system?.label} can also provide — use it for these too?</label>
          {otherEntities.map((e) => (
            <label key={e} style={{ fontWeight: 400 }}>
              <input type="checkbox" checked={also[e] !== false} onChange={(ev) => setAlso({ ...also, [e]: ev.target.checked })} /> {label(e)}
            </label>
          ))}
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

function allPresetEntities(catalog: Catalog, preset: string): string[] {
  const out = new Set<string>();
  for (const c of catalog.categories) for (const s of c.systems) if (s.preset === preset) s.provides.forEach((e) => out.add(e));
  return [...out];
}
