import { useEffect, useMemo, useState } from "react";
import { api, FieldSpec, MappingView, SaveResult, SourceInfo, Suggestion, SuggestResult } from "../api";
import { go, label, Notice, SaveFeedback, useLoad } from "../ui";

type Row = { expr: string; map: string; suggestion?: Suggestion };

function toRow(spec: FieldSpec | undefined): Row {
  if (spec === undefined || spec === null) return { expr: "", map: "" };
  if (typeof spec === "object") return { expr: spec.expr, map: Object.entries(spec.map ?? {}).map(([k, v]) => `${k}=${v}`).join(", ") };
  return { expr: typeof spec === "string" ? spec : JSON.stringify(spec), map: "" };
}

function toSpec(row: Row): FieldSpec | undefined {
  const expr = row.expr.trim();
  if (!expr) return undefined;
  const pairs = row.map.split(",").map((p) => p.split("=").map((s) => s.trim())).filter((p) => p.length === 2 && p[0]);
  return pairs.length ? { expr, map: Object.fromEntries(pairs) } : expr;
}

export default function MappingPage({ route }: { route: string }) {
  const [, source, entity] = route.split("/");
  if (!source || !entity) return <PickEntity source={source} />;
  return <EntityMapping key={`${source}/${entity}`} source={source} entity={entity} />;
}

function PickEntity({ source }: { source?: string }) {
  const sources = useLoad(() => api.get<{ sources: SourceInfo[] }>("/sources"));
  const [mapping, setMapping] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const autoMap = () => { // confident suggestions are saved for every source; the rest stays for review here
    setMapping(true);
    api.post<{ mappings: { accepted: string[]; needs_review: string[] }[] }>("/mapping/auto")
      .then((r) => setNote(`Mapped ${r.mappings.reduce((n, m) => n + m.accepted.length, 0)} field(s) automatically; `
        + `${r.mappings.filter((m) => m.needs_review.length).length} data type(s) have fields to review below.`))
      .catch((e: Error) => setNote(e.message)).finally(() => setMapping(false));
  };
  return (
    <>
      <div className="row">
        <h2>Field mapping</h2>
        <span className="spacer" />
        <button onClick={autoMap} disabled={mapping}>{mapping ? "Mapping…" : "Auto-map all fields"}</button>
      </div>
      <p className="lead">Tell the agent which field in each system means what. The AI proposes a mapping from field names, labels, descriptions and sample values; you review and save it.</p>
      <Notice kind="ok">{note}</Notice>
      {sources.error && <Notice kind="bad">{sources.error}</Notice>}
      {sources.data?.sources.filter((s) => !source || s.name === source).map((s) => (
        <div className="card" key={s.name}>
          <h3>{s.name} <span className="muted">({s.preset ?? s.type})</span></h3>
          <div className="row">
            {s.entities.map((e) => <button key={e} onClick={() => go(`mapping/${s.name}/${e}`)}>{label(e)}</button>)}
            {s.entities.length === 0 && <span className="muted">No data types yet — upload a file or connect the source first.</span>}
          </div>
        </div>
      ))}
    </>
  );
}

function EntityMapping({ source, entity }: { source: string; entity: string }) {
  const view = useLoad(() => api.get<MappingView>(`/mapping/${source}/${entity}`));
  const [rows, setRows] = useState<Record<string, Row>>({});
  const [suggest, setSuggest] = useState<SuggestResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ records: Record<string, unknown>[]; signals_affected: Record<string, string[]> } | null>(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (view.data) setRows(Object.fromEntries(Object.keys(view.data.standard).map((f) => [f, toRow(view.data!.current[f])])));
  }, [view.data]);

  const fields = useMemo(() => {
    const out: Record<string, FieldSpec> = {};
    for (const [f, r] of Object.entries(rows)) {
      const spec = toSpec(r);
      if (spec !== undefined) out[f] = spec;
    }
    return out;
  }, [rows]);

  if (view.error) return <><h2>Field mapping</h2><Notice kind="bad">{view.error}</Notice></>;
  if (!view.data) return <p className="muted">Reading sample records from {source}…</p>;
  const v = view.data;
  const sourceFields = v.describe.fields.filter((f) => !f.free_text);

  const runSuggest = (useModel: boolean) => {
    setBusy("suggest");
    setError(null);
    api.post<SuggestResult>(`/mapping/${source}/${entity}/suggest`, { use_model: useModel })
      .then((r) => {
        setSuggest(r);
        setRows((cur) => {
          const next = { ...cur };
          for (const s of r.suggestions) next[s.field] = { ...toRow(s.expr), suggestion: s };
          return next;
        });
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(null));
  };
  const acceptConfident = () =>
    setRows((cur) => {
      const next = { ...cur };
      for (const [f, r] of Object.entries(next)) if (r.suggestion && !r.suggestion.auto_accept) next[f] = { expr: "", map: "" };
      return next;
    });
  const runPreview = () => {
    setBusy("preview");
    api.post<typeof preview>(`/mapping/${source}/${entity}/preview`, { fields }).then(setPreview).catch((e: Error) => setError(e.message)).finally(() => setBusy(null));
  };
  const save = () => {
    setBusy("save");
    api.put<SaveResult>(`/mapping/${source}/${entity}`, { fields }).then(setResult).catch((e: Error) => setError(e.message)).finally(() => setBusy(null));
  };
  const setRow = (f: string, patch: Partial<Row>) => setRows({ ...rows, [f]: { ...rows[f], ...patch } });

  return (
    <>
      <p className="muted small"><a href="#/mapping">Field mapping</a> / {source}</p>
      <h2>{label(entity)} from {source}</h2>
      <p className="lead">{v.entity_description} Read {v.describe.sample_count} sample record(s) and {v.describe.fields.length} field(s).</p>
      {v.read_only && <Notice kind="warn">This data type combines several objects (e.g. tasks + meetings); its mapping is edited in the config file.</Notice>}
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" disabled={!!busy || v.read_only} onClick={() => runSuggest(true)}>{busy === "suggest" ? "Thinking…" : "Suggest with AI"}</button>
        <button disabled={!!busy || v.read_only} onClick={() => runSuggest(false)}>Suggest (rules only)</button>
        {suggest && <button onClick={acceptConfident}>Keep only high-confidence</button>}
        <span className="spacer" />
        <button disabled={!!busy} onClick={runPreview}>Preview</button>
        <button className="primary" disabled={!!busy || v.read_only} onClick={save}>Save mapping</button>
      </div>
      <Notice kind="warn">{suggest?.note}</Notice>
      <Notice kind="bad">{error}</Notice>
      <SaveFeedback result={result} />
      {suggest && suggest.required_missing.length > 0 && <Notice kind="warn">Still needed: {suggest.required_missing.join(", ")}</Notice>}

      <div className="card" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr><th style={{ width: "26%" }}>Standard field</th><th>From your system</th><th style={{ width: "22%" }}>Suggestion</th></tr>
          </thead>
          <tbody>
            {Object.entries(v.standard).map(([f, doc]) => {
              const r = rows[f] ?? { expr: "", map: "" };
              const s = r.suggestion;
              const picked = sourceFields.find((sf) => r.expr.split("|")[0].trim() === sf.name);
              return (
                <tr key={f}>
                  <td>
                    <strong>{f}</strong> <span className="badge">{doc.type}</span>
                    <div className="muted small">{doc.description}</div>
                  </td>
                  <td>
                    <select
                      value={picked?.name ?? ""}
                      disabled={v.read_only}
                      onChange={(e) => setRow(f, { expr: e.target.value ? (doc.type === "date" ? `${e.target.value} | date` : e.target.value) : "" })}
                    >
                      <option value="">— not mapped / custom expression —</option>
                      {sourceFields.map((sf) => (
                        <option key={sf.name} value={sf.name} title={sf.description ?? ""}>
                          {sf.label && sf.label !== sf.name ? `${sf.label} (${sf.name})` : sf.name}
                          {sf.examples.length ? ` — e.g. ${sf.examples.slice(0, 2).join(", ")}` : ""}
                        </option>
                      ))}
                    </select>
                    <input style={{ marginTop: 4 }} placeholder="expression, e.g. Status | in('Closed','Done') | not" value={r.expr}
                      disabled={v.read_only} onChange={(e) => setRow(f, { expr: e.target.value })} />
                    {(r.map || picked?.options?.length) ? (
                      <input style={{ marginTop: 4 }} placeholder={`value map, e.g. ${picked?.options?.slice(0, 2).map((o) => `${o}=?`).join(", ") ?? "P1=urgent"}`}
                        value={r.map} disabled={v.read_only} onChange={(e) => setRow(f, { map: e.target.value })} />
                    ) : null}
                    {picked?.description && <div className="muted small">{picked.description}</div>}
                  </td>
                  <td className="small">
                    {s ? (
                      <>
                        <span className={s.confidence >= 0.85 ? "conf-hi" : s.confidence >= 0.6 ? "conf-mid" : "conf-lo"}>
                          {Math.round(s.confidence * 100)}% · {s.by}
                        </span>
                        <div className="muted">{s.reason}</div>
                        {s.warnings.map((w) => <div key={w} className="conf-lo">{w}</div>)}
                      </>
                    ) : <span className="muted">—</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {preview && (
        <div className="card">
          <h3>Preview — how {source} records will look to the agent</h3>
          {Object.keys(preview.signals_affected).length > 0 && (
            <Notice kind="warn">
              These signals need fields that are not mapped yet:{" "}
              {Object.entries(preview.signals_affected).map(([sig, f]) => `${sig} (${f.join(", ")})`).join("; ")}
            </Notice>
          )}
          {preview.records.length === 0 ? <p className="muted">No record had an id with this mapping — map the id field.</p> : (
            <pre>{JSON.stringify(preview.records, null, 2)}</pre>
          )}
        </div>
      )}
    </>
  );
}
