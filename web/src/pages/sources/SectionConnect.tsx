// Connect a custom section's data type: an object in a Salesforce org, a CSV
// upload, a SQL query, a REST endpoint or an MCP tool — one request per account.
import { useState } from "react";
import { api, Category, SaveResult } from "../../api";
import { go, Modal, Notice } from "../../ui";
import CsvModal from "./CsvModal";

type Kind = "salesforce" | "csv" | "sql" | "rest" | "mcp";
const KINDS: Record<Kind, string> = { salesforce: "Salesforce object", csv: "CSV / Excel upload", sql: "Database (SQL)", rest: "REST API", mcp: "MCP server" };
const HELP: Record<Exclude<Kind, "csv">, string> = {
  salesforce: "Any standard or custom object in the connected org, read-only.",
  sql: "One read-only SELECT per account. Use :link_value for the account id (and :as_of for the assessment date if you like).",
  rest: "One GET per account. Use {link_value} in the path or parameters for the account id.",
  mcp: "One tool call per account. Use {link_value} in the tool arguments for the account id. Only this tool is ever called.",
};

const parseJson = (text: string): Record<string, unknown> | null => {
  if (!text.trim()) return {};
  try { const v = JSON.parse(text); return v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { return null; }
};

interface Props { category: Category; salesforce: string[]; onClose: () => void; onSaved: (r: SaveResult, name: string, oauth: boolean) => void }

export default function SectionConnect({ category, salesforce: sf, onClose, onSaved }: Props) {
  const entity = category.entities[0];
  const [kind, setKind] = useState<Kind>(sf.length ? "salesforce" : "csv");
  const [name, setName] = useState(sf[0] ?? `${category.key}_data`);
  const [f, setF] = useState<Record<string, string>>({ account_field: "AccountId", auth_type: "bearer", transport: "command" });
  const [accountFields, setAccountFields] = useState<string[]>([]);
  const [testData, setTestData] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });

  const pick = (k: Kind) => { setKind(k); setName(k === "salesforce" ? sf[0] ?? "" : `${category.key}_${k === "csv" ? "data" : k}`); setError(""); };
  const picker = (
    <>
      <label>Where does this data live?</label>
      <select value={kind} onChange={(e) => pick(e.target.value as Kind)} aria-label="connection kind">
        {(Object.keys(KINDS) as Kind[]).filter((k) => k !== "salesforce" || sf.length).map((k) => <option key={k} value={k}>{KINDS[k]}</option>)}
      </select>
    </>
  );
  if (kind === "csv") return <CsvModal category={category} picker={picker} name={name} setName={setName} onClose={onClose} onSaved={onSaved} />;

  const loadObject = () =>
    api.get<{ fields: { name: string; type?: string }[] }>(`/sources/${name}/objects/${encodeURIComponent(f.object ?? "")}/fields`)
      .then((r) => {
        const refs = r.fields.filter((x) => /reference|lookup/i.test(x.type ?? "") || /account/i.test(x.name)).map((x) => x.name);
        setAccountFields(refs);
        setF({ ...f, account_field: refs.find((x) => /account/i.test(x)) ?? refs[0] ?? f.account_field });
        setError("");
      })
      .catch((e: Error) => setError(e.message));

  const save = () => {
    const params = parseJson(f.params ?? ""), toolArgs = parseJson(f.tool_args ?? ""), env = parseJson(f.env ?? "");
    if (params === null || toolArgs === null || env === null) { setError("Parameters, tool arguments and environment must be JSON objects, e.g. {\"account\": \"{link_value}\"}"); return; }
    const body = {
      kind, source: name, test_data: testData, object: f.object, account_field: f.account_field, dsn: f.dsn, query: f.query,
      base_url: f.base_url, auth_type: f.auth_type, auth_name: f.auth_name, secret: f.secret, path: f.path, params, records_path: f.records_path,
      command: f.transport === "command" ? f.command : "", args: (f.args ?? "").split(" ").filter(Boolean), url: f.transport === "url" ? f.url : "",
      env, tool: f.tool, tool_args: toolArgs,
    };
    setBusy(true); setError("");
    api.post<SaveResult>(`/sections/${category.key}/connect`, body)
      .then((r) => { onSaved(r, name, false); go(`mapping/${name}/${entity}`); })
      .catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };

  const input = (k: string, labelText: string, placeholder = "", secret = false) => (
    <div className="field">
      <label>{labelText}</label>
      <input type={secret ? "password" : "text"} autoComplete="off" value={f[k] ?? ""} onChange={set(k)} aria-label={labelText}
        placeholder={secret ? "stored encrypted; never written to config files" : placeholder} />
    </div>
  );

  return (
    <Modal title={`Connect: ${category.label}`} onClose={onClose}>
      {picker}
      <p className="muted small">{HELP[kind]}</p>
      {kind === "salesforce" ? (
        <>
          {sf.length > 1 && <><label>Salesforce connection</label><select value={name} onChange={(e) => setName(e.target.value)}>{sf.map((n) => <option key={n}>{n}</option>)}</select></>}
          <div className="row">
            <div className="field" style={{ flex: 2 }}>{input("object", "Object API name", "Survey__c")}</div>
            <button onClick={loadObject} disabled={!f.object} style={{ alignSelf: "flex-end", marginBottom: 6 }}>Load fields</button>
          </div>
          <div className="field">
            <label>Field that points to the account</label>
            {accountFields.length ? (
              <select value={f.account_field} onChange={set("account_field")} aria-label="account field">{accountFields.map((x) => <option key={x}>{x}</option>)}</select>
            ) : <input value={f.account_field} onChange={set("account_field")} aria-label="account field" placeholder="Account__c" />}
          </div>
        </>
      ) : (
        <>
          <label>Source name <span className="hint">— a short id</span></label>
          <input value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} aria-label="source name" />
        </>
      )}
      {kind === "sql" && (
        <>
          {input("dsn", "Connection string (DSN)", "", true)}
          <div className="field"><label>Query</label>
            <textarea rows={4} value={f.query ?? ""} onChange={set("query")} aria-label="query"
              placeholder={"SELECT id, account_id, score, responded_at\nFROM surveys\nWHERE account_id = :link_value"} /></div>
        </>
      )}
      {kind === "rest" && (
        <>
          {input("base_url", "Base URL", "https://api.example.com")}
          <div className="row">
            <div className="field" style={{ flex: 1 }}><label>Sign-in</label>
              <select value={f.auth_type} onChange={set("auth_type")} aria-label="auth type">
                <option value="none">none</option><option value="bearer">bearer token</option><option value="header">API key header</option>
                <option value="query">API key parameter</option><option value="basic">username + password</option>
              </select></div>
            {["header", "query", "basic"].includes(f.auth_type) && <div style={{ flex: 1 }}>{input("auth_name", f.auth_type === "basic" ? "Username" : "Header / parameter name", "X-API-Key")}</div>}
            {f.auth_type !== "none" && <div style={{ flex: 1 }}>{input("secret", f.auth_type === "basic" ? "Password" : "Token / key", "", true)}</div>}
          </div>
          {input("path", "Path", "/v1/responses")}
          {input("params", "Query parameters (JSON)", "{\"account\": \"{link_value}\"}")}
          {input("records_path", "Where the list sits in the response", "data")}
        </>
      )}
      {kind === "mcp" && (
        <>
          <div className="field"><label>Server</label>
            <select value={f.transport} onChange={set("transport")} aria-label="transport">
              <option value="command">a local command (stdio)</option><option value="url">a URL (streamable HTTP)</option>
            </select></div>
          {f.transport === "command" ? (
            <div className="row">
              <div style={{ flex: 1 }}>{input("command", "Command", "npx")}</div>
              <div style={{ flex: 2 }}>{input("args", "Arguments (space separated)", "-y @acme/survey-mcp")}</div>
            </div>
          ) : input("url", "Server URL", "https://mcp.example.com/mcp")}
          {input("env", "Environment variables (JSON, stored encrypted)", "{\"SURVEY_TOKEN\": \"…\"}")}
          <div className="row">
            <div style={{ flex: 1 }}>{input("tool", "Tool to call", "list_responses")}</div>
            <div style={{ flex: 2 }}>{input("tool_args", "Tool arguments (JSON)", "{\"account\": \"{link_value}\"}")}</div>
          </div>
          {input("records_path", "Where the list sits in the result", "items")}
        </>
      )}
      {kind !== "salesforce" && (
        <label className="inline"><input type="checkbox" checked={testData} onChange={(e) => setTestData(e.target.checked)} /> Synthetic test data only</label>
      )}
      <Notice kind="bad">{error}</Notice>
      <div className="row" style={{ marginTop: 12 }}>
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save} disabled={busy || !name}>{busy ? "Saving…" : "Save & map fields"}</button>
      </div>
    </Modal>
  );
}
