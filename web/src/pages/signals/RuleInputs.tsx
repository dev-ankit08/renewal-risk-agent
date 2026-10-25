// The signal form, generated from the rule table the back end publishes
// (GET /signals/builder): each rule lists its inputs and their kind, so adding a
// rule type on the server needs no change here.
import { useState } from "react";
import { Builder, RuleInput } from "../../api";
import { label } from "../../ui";

type Spec = Record<string, unknown>;
type Cond = { field: string; op: string; value: string };

const NAMES: Record<string, string> = {
  entity: "Records of type", where: "Only records where", window_days: "Window (days)", date_field: "Date field",
  field: "Value field", agg: "Combine values by", baseline_windows: "Earlier periods to average", start_field: "Start date",
  end_field: "End date", current: "Current value", baseline: "Compared with", numerator: "Divide", denominator: "By",
  trigger: "When there are", expected: "But no", cases: "Cases (first match wins)", severity: "Severity", thresholds: "Thresholds",
};
const AGGS: Record<string, string> = { sum: "total", avg: "average", count: "count of records", "latest:": "latest value", "max:": "highest value", "min:": "lowest value" };
const NO_VALUE = new Set(["null", "before_as_of"]);
const DATE_TYPES = new Set(["date", "datetime"]);

// ---- where clause <-> editable rows -----------------------------------------

function toRows(where: unknown): Cond[] {
  const rows: Cond[] = [];
  for (const [field, cond] of Object.entries((where as Spec) ?? {})) {
    if (Array.isArray(cond)) rows.push({ field, op: "in", value: cond.join(", ") });
    else if (cond !== null && typeof cond === "object")
      for (const [op, v] of Object.entries(cond as Spec)) rows.push({ field, op, value: Array.isArray(v) ? v.join(", ") : String(v) });
    else rows.push({ field, op: "eq", value: String(cond) });
  }
  return rows;
}

function parseValue(op: string, raw: string): unknown {
  if (NO_VALUE.has(op)) return raw !== "false";
  if (op === "in" || op === "not_in") return raw.split(",").map((x) => parseValue("eq", x.trim()));
  if (raw === "true" || raw === "false") return raw === "true";
  return raw.trim() !== "" && !isNaN(Number(raw)) ? Number(raw) : raw;
}

function toWhere(rows: Cond[]): Spec | undefined {
  const where: Spec = {};
  for (const r of rows.filter((r) => r.field)) {
    const v = parseValue(r.op, r.value);
    const prev = where[r.field];
    if (r.op === "eq" && prev === undefined) where[r.field] = v;
    else where[r.field] = { ...(prev !== null && typeof prev === "object" && !Array.isArray(prev) ? (prev as Spec) : prev === undefined ? {} : { eq: prev }), [r.op]: v };
  }
  return Object.keys(where).length ? where : undefined;
}

function WhereEditor({ value, fields, ops, onChange }: { value: unknown; fields: string[]; ops: Record<string, string>; onChange: (w: Spec | undefined) => void }) {
  const [rows, setRows] = useState<Cond[]>(() => toRows(value));
  const update = (next: Cond[]) => { setRows(next); onChange(toWhere(next)); };
  const set = (i: number, patch: Partial<Cond>) => update(rows.map((r, j) => (i === j ? { ...r, ...patch } : r)));
  return (
    <div>
      {rows.map((r, i) => (
        <div className="row cond" key={i}>
          <select value={r.field} onChange={(e) => set(i, { field: e.target.value })} aria-label="condition field">
            <option value="">field…</option>
            {fields.map((f) => <option key={f} value={f}>{label(f)}</option>)}
          </select>
          <select value={r.op} onChange={(e) => set(i, { op: e.target.value })} aria-label="condition operator">
            {Object.entries(ops).map(([k, text]) => <option key={k} value={k}>{text}</option>)}
          </select>
          {!NO_VALUE.has(r.op) && <input value={r.value} placeholder={r.op.includes("in") && !r.op.startsWith("within") ? "a, b, c" : "value"} onChange={(e) => set(i, { value: e.target.value })} aria-label="condition value" />}
          <button onClick={() => update(rows.filter((_, j) => j !== i))} aria-label="remove condition">✕</button>
        </div>
      ))}
      <button className="link" onClick={() => update([...rows, { field: "", op: "eq", value: "" }])}>+ condition</button>
    </div>
  );
}

// ---- one input, by kind ---------------------------------------------------

interface Props { inputs: RuleInput[]; spec: Spec; rule: string; builder: Builder; onChange: (s: Spec) => void }

export default function RuleInputs({ inputs, spec, rule, builder, onChange }: Props) {
  const entity = Array.isArray(spec.entity) ? String(spec.entity[0] ?? "") : String(spec.entity ?? "");
  const docs = builder.entities[entity] ?? {};
  const fieldNames = Object.keys(docs);
  const set = (key: string, value: unknown) => {
    const next = { ...spec };
    if (value === undefined || value === "") delete next[key]; else next[key] = value;
    onChange(next);
  };
  const fieldSelect = (key: string, filter: (type: string) => boolean) => (
    <select value={String(spec[key] ?? "")} onChange={(e) => set(key, e.target.value)} aria-label={NAMES[key] ?? key}>
      <option value="">{entity ? "choose…" : "pick the records first"}</option>
      {fieldNames.filter((f) => filter(docs[f].type)).map((f) => <option key={f} value={f} title={docs[f].description}>{label(f)}</option>)}
    </select>
  );

  const render = (inp: RuleInput) => {
    const value = spec[inp.key];
    switch (inp.kind) {
      case "entity":
        return Array.isArray(value)
          ? <input value={value.join(", ")} onChange={(e) => set("entity", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
          : (
            <select value={entity} onChange={(e) => set("entity", e.target.value)} aria-label="record type">
              <option value="">choose…</option>
              {Object.keys(builder.entities).map((e) => <option key={e} value={e}>{label(e)}</option>)}
            </select>
          );
      case "where":
        return <WhereEditor value={value} fields={fieldNames} ops={builder.operators} onChange={(w) => set("where", w)} />;
      case "number":
        return <input type="number" min={1} value={value === undefined ? "" : String(value)} onChange={(e) => set(inp.key, e.target.value === "" ? undefined : Number(e.target.value))} aria-label={NAMES[inp.key] ?? inp.key} />;
      case "date_field":
        return fieldSelect(inp.key, (t) => DATE_TYPES.has(t));
      case "field":
        return fieldSelect(inp.key, (t) => !DATE_TYPES.has(t));
      case "agg": {
        const current = String(value ?? "sum");
        const prefix = current.includes(":") ? current.split(":")[0] + ":" : current;
        return (
          <div className="row">
            <select value={prefix} onChange={(e) => set("agg", e.target.value.endsWith(":") ? e.target.value + (fieldNames.find((f) => DATE_TYPES.has(docs[f].type)) ?? "created_at") : e.target.value)} aria-label="aggregate">
              {Object.entries(AGGS).map(([k, text]) => <option key={k} value={k}>{text}</option>)}
            </select>
            {current.includes(":") && <span className="muted small">by {label(current.split(":")[1])}</span>}
          </div>
        );
      }
      case "severity":
        return (
          <select value={String(value ?? "medium")} onChange={(e) => set(inp.key, e.target.value)} aria-label="severity">
            {builder.severities.map((s) => <option key={s}>{s}</option>)}
          </select>
        );
      case "thresholds": {
        const th = (value as Record<string, number>) ?? {};
        const changeRule = rule === "pct_change" || rule === "window_change";
        const dirs = changeRule ? { decrease: "a drop", increase: "a rise" } : { above: "at or above", below: "at or below" };
        return (
          <div className="row">
            <select value={String(spec.direction ?? (changeRule ? "decrease" : rule === "ratio" ? "below" : "above"))} onChange={(e) => set("direction", e.target.value)} aria-label="direction">
              {Object.entries(dirs).map(([k, text]) => <option key={k} value={k}>{text}</option>)}
            </select>
            {["medium", "high"].map((lvl) => (
              <label key={lvl} className="inline">{lvl}
                <input type="number" step="any" style={{ width: 90 }} value={th[lvl] ?? ""} aria-label={`${lvl} threshold`}
                  onChange={(e) => set("thresholds", { ...th, [lvl]: e.target.value === "" ? undefined : Number(e.target.value) })} />
              </label>
            ))}
          </div>
        );
      }
      case "group":
        return (
          <div className="group">
            <RuleInputs inputs={inp.inputs ?? []} spec={(value as Spec) ?? {}} rule={rule} builder={builder} onChange={(g) => set(inp.key, g)} />
          </div>
        );
      case "cases": {
        const cases = (value as Spec[]) ?? [];
        const setCase = (i: number, c: Spec) => set("cases", cases.map((x, j) => (i === j ? c : x)));
        return (
          <div>
            {cases.map((c, i) => (
              <div className="group" key={i}>
                <div className="row">
                  <select value={String(c.severity ?? "medium")} onChange={(e) => setCase(i, { ...c, severity: e.target.value })} aria-label="case severity">
                    {builder.severities.map((s) => <option key={s}>{s}</option>)}
                  </select>
                  <span className="muted small">if any record also matches:</span>
                  <span className="spacer" />
                  <button onClick={() => set("cases", cases.filter((_, j) => j !== i))} aria-label="remove case">✕</button>
                </div>
                <WhereEditor value={c.where} fields={fieldNames} ops={builder.operators} onChange={(w) => setCase(i, { ...c, where: w ?? {} })} />
              </div>
            ))}
            <button className="link" onClick={() => set("cases", [...cases, { severity: "medium", where: {} }])}>+ case</button>
          </div>
        );
      }
    }
  };

  return (
    <>
      {inputs.map((inp) => (
        <div key={inp.key} className="field">
          <label>{NAMES[inp.key] ?? label(inp.key)}{inp.optional && <span className="hint"> — optional</span>}</label>
          {render(inp)}
        </div>
      ))}
    </>
  );
}
