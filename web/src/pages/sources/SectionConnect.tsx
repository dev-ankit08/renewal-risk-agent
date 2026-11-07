// Connect a custom section's data type to a REST endpoint or an MCP tool — one
// request per account. (Salesforce objects, tables and CSV files are picked on the
// section's data-type row instead.)
import { useState } from "react";
import { api, Category, SaveResult } from "../../api";
import { Field, go, Modal, Notice } from "../../ui";

type Kind = "rest" | "mcp";
const KINDS: Record<Kind, string> = { rest: "REST API", mcp: "MCP server" };
const HELP: Record<Kind, string> = {
  rest: "One GET per account. Use {link_value} in the path or parameters for the account id.",
  mcp: "One tool call per account. Use {link_value} in the tool arguments for the account id. Only this tool is ever called.",
};

const parseJson = (text: string): Record<string, unknown> | null => {
  if (!text.trim()) return {};
  try { const v = JSON.parse(text); return v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { return null; }
};

interface Props { category: Category; onClose: () => void; onSaved: (r: SaveResult, name: string) => void }

export default function SectionConnect({ category, onClose, onSaved }: Props) {
  const entity = category.entities[0];
  const [kind, setKind] = useState<Kind>("rest");
  const [name, setName] = useState(`${category.key}_rest`);
  const [f, setF] = useState<Record<string, string>>({ auth_type: "bearer", transport: "command" });
  const [testData, setTestData] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });

  const save = () => {
    const params = parseJson(f.params ?? ""), toolArgs = parseJson(f.tool_args ?? ""), env = parseJson(f.env ?? "");
    if (params === null || toolArgs === null || env === null) { setError("Parameters, tool arguments and environment must be JSON objects, e.g. {\"account\": \"{link_value}\"}"); return; }
    const body = {
      kind, source: name, test_data: testData, base_url: f.base_url, auth_type: f.auth_type, auth_name: f.auth_name, secret: f.secret,
      path: f.path, params, records_path: f.records_path, command: f.transport === "command" ? f.command : "",
      args: (f.args ?? "").split(" ").filter(Boolean), url: f.transport === "url" ? f.url : "", env, tool: f.tool, tool_args: toolArgs,
    };
    setBusy(true); setError("");
    api.post<SaveResult>(`/sections/${category.key}/connect`, body)
      .then((r) => { onSaved(r, name); go(`mapping/${name}/${entity}`); })
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
      <Field label="Where does this data live?" tip={HELP[kind]}>
        <select value={kind} onChange={(e) => { setKind(e.target.value as Kind); setName(`${category.key}_${e.target.value}`); }} aria-label="connection kind">
          {(Object.keys(KINDS) as Kind[]).map((k) => <option key={k} value={k}>{KINDS[k]}</option>)}
        </select>
      </Field>
      <Field label="Source name" tip="A short id, e.g. surveys_api.">
        <input value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} aria-label="source name" />
      </Field>
      {kind === "rest" ? (
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
      ) : (
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
      <label className="inline"><input type="checkbox" checked={testData} onChange={(e) => setTestData(e.target.checked)} /> Synthetic test data only</label>
      <Notice kind="bad">{error}</Notice>
      <div className="row" style={{ marginTop: 12 }}>
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save} disabled={busy || !name}>{busy ? "Saving…" : "Save & map fields"}</button>
      </div>
    </Modal>
  );
}
