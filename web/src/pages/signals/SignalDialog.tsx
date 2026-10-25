// Create / edit / duplicate a custom signal: describe it (AI draft) -> build it
// (form from the rule table, live explanation) -> test it on real accounts -> save.
import { useEffect, useState } from "react";
import { api, Builder, SignalSpec, SignalTest } from "../../api";
import { Modal, Notice, useLoad } from "../../ui";
import RuleInputs from "./RuleInputs";

const META = ["name", "description", "specialist", "rule", "how_calculated_note", "value_template", "enabled", "custom", "author", "created_at", "updated_at"];

// Keep only the keys that belong to the chosen rule (switching rule type drops the rest).
function ruleSpec(spec: SignalSpec, builder: Builder): SignalSpec {
  const keys = new Set([...META, "direction", ...(builder.rules[spec.rule ?? ""]?.inputs.map((i) => i.key) ?? [])]);
  return Object.fromEntries(Object.entries(spec).filter(([k]) => keys.has(k))) as SignalSpec;
}

interface Props { initial: SignalSpec | null; originalName: string | null; onClose: () => void; onSaved: () => void }

export default function SignalDialog({ initial, originalName, onClose, onSaved }: Props) {
  const builder = useLoad(() => api.get<Builder>("/signals/builder"));
  const [spec, setSpec] = useState<SignalSpec>(initial ?? { name: "", rule: "count", thresholds: { medium: 2, high: 3 } });
  const [formKey, setFormKey] = useState(0); // remounts the form after an AI draft replaces the spec
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<"" | "draft" | "test" | "save">("");
  const [draftNote, setDraftNote] = useState("");
  const [preview, setPreview] = useState<{ explanation: string; errors: string[] } | null>(null);
  const [test, setTest] = useState<SignalTest | null>(null);
  const [error, setError] = useState("");
  const [json, setJson] = useState<string | null>(null);

  const b = builder.data;
  useEffect(() => {
    if (b && !spec.specialist) setSpec((s) => ({ ...s, specialist: b.specialists[0] }));
  }, [b]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live "how it's calculated" — debounced, pure computation on the server.
  useEffect(() => {
    const t = setTimeout(() => {
      api.post<{ explanation: string; errors: string[] }>("/signals/preview", { signal: spec, original_name: originalName })
        .then(setPreview).catch(() => undefined);
    }, 400);
    return () => clearTimeout(t);
  }, [spec, originalName]);

  const run = async (kind: "draft" | "test" | "save", fn: () => Promise<void>) => {
    setBusy(kind); setError("");
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };
  const draft = () => run("draft", async () => {
    const r = await api.post<{ signal: SignalSpec; reason: string }>("/signals/draft", { text });
    setSpec({ ...r.signal, how_calculated_note: spec.how_calculated_note }); setDraftNote(r.reason); setTest(null); setFormKey((k) => k + 1);
  });
  const runTest = () => run("test", async () => setTest(await api.post<SignalTest>("/signals/test", { signal: spec })));
  const save = () => run("save", async () => {
    await api.post("/signals/custom", { signal: spec, original_name: originalName });
    onSaved();
  });
  const applyJson = () => {
    try { setSpec(JSON.parse(json ?? "{}")); setJson(null); setFormKey((k) => k + 1); } catch { setError("That is not valid JSON."); }
  };

  const rule = b?.rules[spec.rule ?? ""];
  return (
    <Modal title={originalName ? `Edit ${originalName}` : "New signal"} onClose={onClose}>
      <Notice kind="bad">{builder.error || error}</Notice>
      {b && (
        <>
          <div className="step"><span className="badge">1</span> Describe the warning sign</div>
          <div className="row">
            <input style={{ flex: 1 }} value={text} onChange={(e) => setText(e.target.value)} aria-label="describe the signal"
              placeholder="e.g. flag accounts with 3 or more open urgent tickets in the last 30 days" />
            <button onClick={draft} disabled={busy !== "" || text.trim().length < 5}>{busy === "draft" ? "Drafting…" : "Draft with AI"}</button>
          </div>
          {draftNote && <p className="muted small">AI: {draftNote} Review the form below before saving.</p>}

          <div className="step"><span className="badge">2</span> Build it</div>
          <div key={formKey}>
            <div className="row">
              <div className="field" style={{ flex: 2 }}>
                <label>Name <span className="hint">— lowercase_with_underscores</span></label>
                <input value={spec.name} onChange={(e) => setSpec({ ...spec, name: e.target.value })} aria-label="signal name" />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Specialist</label>
                <select value={spec.specialist ?? ""} onChange={(e) => setSpec({ ...spec, specialist: e.target.value })} aria-label="specialist">
                  {b.specialists.map((s) => <option key={s}>{s}</option>)}
                </select>
              </div>
            </div>
            <div className="field">
              <label>Description <span className="hint">— what this warning sign means for the account</span></label>
              <input value={spec.description ?? ""} onChange={(e) => setSpec({ ...spec, description: e.target.value })} aria-label="signal description" />
            </div>
            <div className="field">
              <label>What to measure</label>
              <select value={spec.rule} onChange={(e) => setSpec(ruleSpec({ ...spec, rule: e.target.value }, b))} aria-label="rule type">
                {Object.entries(b.rules).map(([k, r]) => <option key={k} value={k}>{r.label}</option>)}
              </select>
              {rule && <div className="muted small">{rule.summary}</div>}
            </div>
            {rule && json === null && <RuleInputs inputs={rule.inputs} spec={spec} rule={spec.rule ?? ""} builder={b} onChange={(s) => setSpec(s as SignalSpec)} />}
            {json !== null && (
              <div className="field">
                <textarea rows={12} value={json} onChange={(e) => setJson(e.target.value)} aria-label="signal JSON" />
                <div className="row"><button onClick={applyJson}>Apply JSON</button><button className="link" onClick={() => setJson(null)}>Cancel</button></div>
              </div>
            )}
            <div className="field">
              <label>Message shown to users <span className="hint">— optional, e.g. "{"{count}"} urgent tickets open"</span></label>
              <input value={spec.value_template ?? ""} onChange={(e) => setSpec({ ...spec, value_template: e.target.value || undefined })} aria-label="message template" />
            </div>
            <div className="field">
              <label>Your note on how it's calculated <span className="hint">— optional, shown under the generated explanation</span></label>
              <textarea rows={2} value={spec.how_calculated_note ?? ""} onChange={(e) => setSpec({ ...spec, how_calculated_note: e.target.value })} aria-label="how it is calculated note" />
            </div>
            {json === null && <button className="link" onClick={() => setJson(JSON.stringify(spec, null, 2))}>Advanced: edit as JSON</button>}
          </div>

          <div className="explain">
            <strong>How it's calculated</strong>
            <div>{preview?.explanation ?? "…"}</div>
            {spec.how_calculated_note && <div className="muted small">{spec.how_calculated_note}</div>}
          </div>
          {preview && preview.errors.length > 0 && <Notice kind="warn"><ul>{preview.errors.map((e) => <li key={e}>{e}</li>)}</ul></Notice>}

          <div className="step"><span className="badge">3</span> Test it on accounts renewing soon <span className="hint">— real data, no AI cost</span></div>
          <button onClick={runTest} disabled={busy !== ""}>{busy === "test" ? "Testing…" : "Test signal"}</button>
          {test && (
            <div style={{ marginTop: 8 }}>
              <p className="small"><strong>{test.fired}</strong> of {test.tested} accounts would be flagged.</p>
              <table>
                <thead><tr><th>Account</th><th>Result</th><th>Value</th></tr></thead>
                <tbody>
                  {test.results.map((r) => (
                    <tr key={r.account_id}>
                      <td>{r.name ?? r.account_id} <span className="muted small">{r.account_id}</span></td>
                      <td>{r.fired ? <span className={`badge ${r.severity === "high" ? "bad" : "warn"}`}>{r.severity}</span> : <span className="badge">no</span>}</td>
                      <td className="small">{r.value || r.gaps.join("; ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="row" style={{ marginTop: 16 }}>
            <span className="spacer" />
            <button onClick={onClose}>Cancel</button>
            <button className="primary" onClick={save} disabled={busy !== "" || (preview?.errors.length ?? 0) > 0}>{busy === "save" ? "Saving…" : "Save signal"}</button>
          </div>
        </>
      )}
    </Modal>
  );
}
