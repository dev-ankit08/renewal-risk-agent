import { useState } from "react";
import { api, Linking, SaveResult } from "../api";
import { Notice, SaveFeedback, useLoad } from "../ui";

const RULES = [
  { key: "key", label: "They store our account id", rule: { match: "key_account_id = account.id" } },
  { key: "domain", label: "Same email / web domain", rule: { match: "domain" } },
  { key: "fuzzy", label: "Similar names (you review each)", rule: { match: "fuzzy_name", threshold: 0.88 } },
  { key: "same", label: "They already use our account ids", rule: { match: "same_id" } },
];

export default function MatchingPage() {
  const linking = useLoad(() => api.get<Linking>("/linking"));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rebuild = () => {
    setBusy(true);
    api.post<Linking>("/linking/rebuild").then((r) => linking.setData({ ...r, rules: linking.data?.rules })).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  const decide = (source: string, account_id: string, link_value: string, status: string) =>
    api.post<Linking>("/linking/decision", { source, account_id, link_value, status }).then((r) => linking.setData({ ...r, rules: linking.data?.rules }));
  const saveRules = (source: string, keys: string[]) =>
    api.put<SaveResult>(`/linking/${source}`, { rules: RULES.filter((r) => keys.includes(r.key)).map((r) => r.rule) })
      .then((r) => { setResult(r); linking.reload(); });

  const d = linking.data;
  return (
    <>
      <div className="row">
        <div>
          <h2>Account matching</h2>
          <p className="lead">How each system's customers line up with your primary account list.</p>
        </div>
        <span className="spacer" />
        <button disabled={busy} onClick={rebuild}>{busy ? "Matching…" : "Re-run matching"}</button>
      </div>
      <Notice kind="bad">{linking.error || error}</Notice>
      <SaveFeedback result={result} />
      {d?.note && <Notice>{d.note}</Notice>}
      {d && Object.entries(d.sources).map(([name, s]) => {
        const configured = (d.rules?.[name] as { match: string }[] | undefined)?.map((r) => RULES.find((x) => x.rule.match === r.match)?.key ?? "");
        // No rules configured = the back end's defaults: same ids, or domain + similar names.
        const current = configured ?? (s.method === "same_id" ? ["same"] : ["domain", "fuzzy"]);
        return (
          <div className="card" key={name}>
            <div className="row">
              <h3>{name}</h3>
              {s.error ? <span className="badge bad">error</span> : (
                <span className={`badge ${s.coverage >= 0.8 ? "ok" : "warn"}`}>{Math.round(s.coverage * 100)}% matched ({s.linked}/{d.accounts})</span>
              )}
              <span className="spacer" />
              <span className="muted small">{s.method === "same_id" ? "uses the same ids" : `${s.customers} customers in ${name}`}</span>
            </div>
            {s.error && <Notice kind="bad">{s.error}</Notice>}
            <div className="meter"><span style={{ width: `${Math.round(s.coverage * 100)}%` }} /></div>
            <label>Match by (in this order)</label>
            <div className="row">
              {RULES.map((r) => (
                <label key={r.key} style={{ fontWeight: 400, margin: 0 }}>
                  <input type="checkbox" defaultChecked={current.includes(r.key)}
                    onChange={(e) => saveRules(name, e.target.checked ? [...current, r.key] : current.filter((k) => k !== r.key))} /> {r.label}
                </label>
              ))}
            </div>
            {s.review.length > 0 && (
              <>
                <h4>Suggested matches to review</h4>
                <table>
                  <thead><tr><th>{name} customer</th><th>Your account</th><th>Why</th><th /></tr></thead>
                  <tbody>
                    {s.review.map((r) => (
                      <tr key={`${r.customer_id}-${r.account_id}`}>
                        <td>{r.customer_name ?? r.customer_id} <code>{r.customer_id}</code></td>
                        <td>{r.account_name ?? r.account_id}</td>
                        <td className="muted">{r.reason}{r.score ? ` (${Math.round(r.score * 100)}%)` : ""}</td>
                        <td className="row">
                          <button className="primary" onClick={() => decide(name, r.account_id, r.customer_id, "approved")}>Match</button>
                          <button onClick={() => decide(name, r.account_id, r.customer_id, "rejected")}>Not a match</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
            {s.unmatched_customers.length > 0 && (
              <details>
                <summary className="muted small">{s.unmatched_customers.length} {name} customer(s) not matched to any account</summary>
                <p className="small">{s.unmatched_customers.slice(0, 50).map((u) => u.name ?? u.id).join(", ")}</p>
              </details>
            )}
          </div>
        );
      })}
    </>
  );
}
