// AI agents: the coordinator, one specialist per data section, and the synthesis
// agent. Each one's instructions are editable; the safety rules are fixed.
import { useEffect, useState } from "react";
import { AgentInfo, Agents, api, Catalog, SaveResult } from "../api";
import { go, label, Notice, SaveFeedback, useLoad } from "../ui";
import SectionModal from "./sources/SectionModal";

function AgentCard({ title, subtitle, agent, badges, openPrompt = false, onSaved }: {
  title: string; subtitle: string; agent: AgentInfo; badges?: React.ReactNode; openPrompt?: boolean; onSaved: (r: SaveResult) => void;
}) {
  const [text, setText] = useState(agent.instructions);
  const [show, setShow] = useState(openPrompt);
  const [error, setError] = useState("");
  useEffect(() => setText(agent.instructions), [agent.instructions]);
  const save = (value: string) =>
    api.put<SaveResult>(`/agents/${agent.key}`, { instructions: value }).then((r) => { setError(""); onSaved(r); }).catch((e: Error) => setError(e.message));

  return (
    <div className="card agent">
      <div className="row"><h3>{title}</h3>{badges}</div>
      <p className="muted small">{subtitle}</p>
      <label>Instructions in use <span className="hint">— edit what this agent focuses on and what matters most for your company</span></label>
      <textarea rows={Math.min(8, Math.max(3, text.split("\n").length + 1))} value={text} onChange={(e) => setText(e.target.value)}
        aria-label={`${agent.key} instructions`} />
      <div className="row">
        <button className="primary" disabled={text === agent.instructions} onClick={() => save(text)}>Save</button>
        {agent.instructions !== agent.default ? <button onClick={() => save("")}>Reset to default</button>
          : <span className="badge">default</span>}
        <span className="spacer" />
        <button className="link" onClick={() => setShow(!show)}>{show ? "Hide" : "Show"} full prompt</button>
      </div>
      {show && <pre className="small">{agent.preview}</pre>}
      <Notice kind="bad">{error}</Notice>
    </div>
  );
}

export default function AgentsPage() {
  const data = useLoad(() => api.get<Agents>("/agents"));
  const catalog = useLoad(() => api.get<Catalog>("/catalog"));
  const [adding, setAdding] = useState(false);
  const [result, setResult] = useState<SaveResult | null>(null);
  const saved = (r: SaveResult) => { setResult(r); data.reload(); };
  const d = data.data;

  return (
    <>
      <h2>AI agents</h2>
      <p className="lead">Every full assessment runs a team: a coordinator picks the specialists that have data, one specialist per
        data section reads only its section, and a synthesis agent turns their findings into the score, reasons and actions.
        Edit each one's instructions here; the safety rules (cite real records, never invent, the answer format) are fixed.</p>
      <Notice kind="bad">{data.error}</Notice>
      <SaveFeedback result={result} />
      {d && !d.ai_enabled && <Notice kind="warn">AI is off (no API key): answers use the free rules today. These instructions apply once a key is added.</Notice>}
      {d && (
        <>
          <AgentCard title="1. Coordinator" subtitle="Looks at which sections have data for the account and decides which specialists to run."
            agent={d.coordinator} openPrompt onSaved={saved} />
          <div className="flow muted small">↓ runs in parallel, one per section with data ↓</div>
          <div className="grid2">
            {d.specialists.map((s) => (
              <AgentCard key={s.key} title={`Specialist: ${s.label}`} agent={s} onSaved={saved}
                subtitle={`Reads ${s.entities.map(label).join(", ")}. Checks ${s.signals.length} warning sign(s): ${s.signals.map(label).join(", ") || "none yet"}.`}
                badges={<>
                  {s.custom && <span className="badge ok">custom section</span>}
                  {s.has_data ? <span className="badge ok">has data</span>
                    : <button className="link small" onClick={() => go("sources")}>no data mapped — skipped</button>}
                </>} />
            ))}
          </div>
          <div className="row">
            <span className="muted small">Each Data sources section is one specialist: add a section to add a specialist for new kinds of data.</span>
            <span className="spacer" />
            <button onClick={() => setAdding(true)}>+ Add section (adds a specialist)</button>
          </div>
          <div className="flow muted small">↓ all findings ↓</div>
          <AgentCard title="3. Synthesis" subtitle="Weighs every specialist's findings and writes the score, the reasons and 2–4 actions (using your playbook)."
            agent={d.synthesis} openPrompt onSaved={saved} />
        </>
      )}
      {adding && (
        <SectionModal section={null} salesforce={catalog.data?.salesforce_sources ?? []} onClose={() => setAdding(false)}
          onSaved={(r) => { setAdding(false); saved(r); catalog.reload(); }} />
      )}
    </>
  );
}
