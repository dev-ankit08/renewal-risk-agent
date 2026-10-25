// The setup checklist: what is left before people get answers, each step one click
// from its screen — plus the two shortcuts, sample data and auto-mapping.
import { useState } from "react";
import { api, Checklist as Data, SaveResult } from "./api";
import { go, Notice, useLoad } from "./ui";

export function useChecklist() {
  return useLoad(() => api.get<Data>("/setup/checklist"));
}

export async function switchWorkspace(name: "demo" | "home") {
  await api.put("/workspace", { name });
  window.location.hash = "#/sources";
  window.location.reload(); // every screen reloads against the other workspace
}

export default function Checklist() {
  const list = useChecklist();
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const d = list.data;
  if (!d) return list.error ? <Notice kind="bad">{list.error}</Notice> : null;
  const complete = d.done === d.total;

  const autoMap = () => {
    setBusy("map");
    api.post<SaveResult & { mappings: { source: string; entity: string; accepted: string[]; needs_review: string[] }[] }>("/mapping/auto")
      .then((r) => {
        const accepted = r.mappings.reduce((n, m) => n + m.accepted.length, 0);
        const review = r.mappings.filter((m) => m.needs_review.length).length;
        setNote(`Mapped ${accepted} field(s) automatically.${review ? ` ${review} data type(s) have fields to review on Field mapping.` : ""}`);
        list.reload();
      })
      .catch((e: Error) => setNote(e.message))
      .finally(() => setBusy(""));
  };

  return (
    <details className="card checklist" open={!complete}>
      <summary>
        <strong>{complete ? "Setup complete" : "Get set up"}</strong>{" "}
        <span className="muted">{d.done} of {d.total} done</span>
        <div className="meter"><span style={{ width: `${(100 * d.done) / d.total}%` }} /></div>
      </summary>
      <ol>
        {d.items.map((i) => (
          <li key={i.key} className={i.done ? "done" : ""}>
            <span aria-hidden>{i.done ? "✓" : "○"}</span> {i.label}
            {!i.done && <> <span className="muted small">— {i.detail}</span> <button className="link" onClick={() => go(i.route)}>Go</button></>}
          </li>
        ))}
      </ol>
      <div className="row">
        <button onClick={autoMap} disabled={busy !== ""}>{busy === "map" ? "Mapping…" : "Auto-map all fields"}</button>
        {d.workspace.active === "home" && d.workspace.demo_available && (
          <button onClick={() => { setBusy("demo"); switchWorkspace("demo").catch((e: Error) => { setNote(e.message); setBusy(""); }); }} disabled={busy !== ""}>
            {busy === "demo" ? "Loading sample data…" : "Try with sample data"}
          </button>
        )}
        <span className="muted small">Sample data opens in a separate demo workspace — your setup is not changed.</span>
      </div>
      <Notice kind="ok">{note}</Notice>
    </details>
  );
}
