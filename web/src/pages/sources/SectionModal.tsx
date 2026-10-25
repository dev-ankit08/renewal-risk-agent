// Create or edit a custom section: its data type (fields the admin describes) and
// the focus of its own specialist. Fields can be started from a CSV header or a
// Salesforce object instead of typed by hand.
import { useState } from "react";
import { api, FieldType, SaveResult, Section, SectionField } from "../../api";
import { Modal, Notice } from "../../ui";

const TYPES: Record<FieldType, string> = { str: "text", num: "number", date: "date", bool: "yes / no", list: "list" };
const STANDARD = new Set(["id", "account_id", "created_at", "updated_at"]);
const FREE_TEXT = /description|subject|body|comment|note|verbatim|message|content|transcript/i;
const SF_SYSTEM = /^(Id|IsDeleted|Name|CreatedDate|CreatedById|LastModifiedDate|LastModifiedById|SystemModstamp|OwnerId|LastActivityDate|LastViewedDate|LastReferencedDate)$/;

export const slug = (text: string) =>
  text.replace(/__c$/i, "").replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^(\d)/, "f_$1").slice(0, 40);

const sfType = (t = ""): FieldType =>
  /double|currency|percent|int|number|long/i.test(t) ? "num" : /date/i.test(t) ? "date" : /boolean|checkbox/i.test(t) ? "bool" : "str";
const guessType = (value: string): FieldType =>
  /^\d{4}-\d{2}-\d{2}/.test(value) ? "date" : value !== "" && !isNaN(Number(value)) ? "num" : /^(true|false|yes|no)$/i.test(value) ? "bool" : "str";

// Keep fields the agent can use: not the standard ones, not free text.
const usable = (fields: SectionField[]) => fields.filter((f) => f.name && !STANDARD.has(f.name) && !FREE_TEXT.test(f.name));

interface Props { section: Section | null; salesforce: string[]; onClose: () => void; onSaved: (r: SaveResult) => void }

export default function SectionModal({ section, salesforce, onClose, onSaved }: Props) {
  const editing = section !== null;
  const [s, setS] = useState<Section>(section ?? {
    key: "", label: "", description: "", focus: "",
    data_type: { name: "", description: "", date_field: "created_at", fields: [{ name: "", type: "str", description: "" }] },
  });
  const [sfSource, setSfSource] = useState(salesforce[0] ?? "");
  const [sfObject, setSfObject] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dt = s.data_type;
  const setDt = (patch: Partial<Section["data_type"]>) => setS({ ...s, data_type: { ...dt, ...patch } });
  const setField = (i: number, patch: Partial<SectionField>) => setDt({ fields: dt.fields.map((f, j) => (i === j ? { ...f, ...patch } : f)) });
  const [named, setNamed] = useState(editing); // stop following the section name once the admin types a type name
  const setLabel = (text: string) =>
    setS({ ...s, label: text, ...(editing ? {} : { key: slug(text), data_type: { ...dt, name: named ? dt.name : slug(text).replace(/s$/, "") } }) });

  const fromCsv = (file: File | undefined) => {
    if (!file) return;
    file.slice(0, 64 * 1024).text().then((text) => {
      const [head = "", first = ""] = text.replace(/^﻿/, "").split(/\r?\n/);
      const cols = head.split(",").map((c) => c.trim().replace(/^"|"$/g, ""));
      const vals = first.split(",").map((v) => v.trim().replace(/^"|"$/g, ""));
      setDt({ fields: usable(cols.map((c, i) => ({ name: slug(c), type: guessType(vals[i] ?? ""), description: c }))) });
    });
  };
  const fromSalesforce = () => {
    setError("");
    api.get<{ fields: { name: string; label?: string; type?: string; description?: string }[] }>(`/sources/${sfSource}/objects/${encodeURIComponent(sfObject)}/fields`)
      .then((r) => setDt({
        fields: usable(r.fields.filter((f) => !SF_SYSTEM.test(f.name) && !/^Account/i.test(f.name))
          .map((f) => ({ name: slug(f.name), type: sfType(f.type), description: f.description || f.label || f.name }))),
      }))
      .catch((e: Error) => setError(e.message));
  };
  const save = () => {
    setBusy(true); setError("");
    api.put<SaveResult>(`/sections/${s.key}`, { label: s.label, description: s.description, data_type: { ...dt, fields: dt.fields.filter((f) => f.name) }, focus: s.focus })
      .then(onSaved).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  const dateFields = dt.fields.filter((f) => f.type === "date" && f.name);

  return (
    <Modal title={editing ? `Edit section: ${s.label}` : "Add a data section"} onClose={onClose}>
      <p className="muted small">A section brings a new kind of data into the agent — e.g. survey answers, product feedback, contract
        health checks — and gets its own specialist that reads it and reports what it finds.</p>
      <div className="row">
        <div className="field" style={{ flex: 2 }}><label>Section name</label>
          <input value={s.label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Customer surveys" aria-label="section name" /></div>
        <div className="field" style={{ flex: 1 }}><label>Key <span className="hint">— fixed once saved</span></label>
          <input value={s.key} disabled={editing} onChange={(e) => setS({ ...s, key: slug(e.target.value) })} aria-label="section key" /></div>
      </div>
      <div className="field"><label>Description</label>
        <input value={s.description} onChange={(e) => setS({ ...s, description: e.target.value })} placeholder="e.g. NPS and CSAT answers from our survey tool" aria-label="section description" /></div>

      <div className="step"><span className="badge">1</span> The data</div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}><label>Data type name <span className="hint">— one record is one…</span></label>
          <input value={dt.name} disabled={editing} onChange={(e) => { setNamed(true); setDt({ name: slug(e.target.value) }); }} placeholder="survey" aria-label="data type name" /></div>
        <div className="field" style={{ flex: 2 }}><label>What one record is</label>
          <input value={dt.description} onChange={(e) => setDt({ description: e.target.value })} placeholder="One survey answer from a customer contact" aria-label="data type description" /></div>
      </div>
      <div className="row small">
        <span className="muted">Start the fields from:</span>
        <label className="inline">a CSV file <input type="file" accept=".csv" onChange={(e) => fromCsv(e.target.files?.[0])} style={{ width: "auto" }} aria-label="fields from csv" /></label>
        {salesforce.length > 0 && (
          <>
            <span className="muted">or a Salesforce object</span>
            {salesforce.length > 1 && <select value={sfSource} onChange={(e) => setSfSource(e.target.value)} style={{ width: "auto" }}>{salesforce.map((n) => <option key={n}>{n}</option>)}</select>}
            <input value={sfObject} onChange={(e) => setSfObject(e.target.value)} placeholder="Survey__c" style={{ width: 140 }} aria-label="salesforce object" />
            <button onClick={fromSalesforce} disabled={!sfObject}>Load fields</button>
          </>
        )}
      </div>
      <table className="fields">
        <thead><tr><th>Field</th><th>Type</th><th>What it means <span className="hint">— the AI maps source fields by this</span></th><th /></tr></thead>
        <tbody>
          {dt.fields.map((f, i) => (
            <tr key={i}>
              <td><input value={f.name} onChange={(e) => setField(i, { name: slug(e.target.value) })} placeholder="score" aria-label="field name" /></td>
              <td><select value={f.type} onChange={(e) => setField(i, { type: e.target.value as FieldType })} aria-label="field type">
                {Object.entries(TYPES).map(([k, text]) => <option key={k} value={k}>{text}</option>)}</select></td>
              <td><input value={f.description} onChange={(e) => setField(i, { description: e.target.value })} placeholder="Likelihood to recommend, 0-10" aria-label="field description" /></td>
              <td><button onClick={() => setDt({ fields: dt.fields.filter((_, j) => j !== i) })} aria-label="remove field">✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="link" onClick={() => setDt({ fields: [...dt.fields, { name: "", type: "str", description: "" }] })}>+ field</button>
      <p className="muted small">Added automatically: id, account id, created at, updated at. Free-text fields (comments, notes…) are left out — they never reach the AI.</p>
      <div className="field"><label>When did it happen? <span className="hint">— the date the agent uses to keep to the assessment date</span></label>
        <select value={dt.date_field} onChange={(e) => setDt({ date_field: e.target.value })} aria-label="date field">
          <option value="created_at">created at (when the record was created)</option>
          {dateFields.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
        </select></div>

      <div className="step"><span className="badge">2</span> Its specialist</div>
      <div className="field"><label>What should the specialist look for?</label>
        <textarea rows={3} value={s.focus} onChange={(e) => setS({ ...s, focus: e.target.value })} aria-label="specialist focus"
          placeholder="e.g. Judge whether the customer is unhappy from their survey scores and how they trend; low scores from senior contacts matter most." /></div>

      <Notice kind="bad">{error}</Notice>
      <div className="row" style={{ marginTop: 12 }}>
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save} disabled={busy || !s.key || !dt.name || !s.focus}>{busy ? "Saving…" : "Save section"}</button>
      </div>
    </Modal>
  );
}
