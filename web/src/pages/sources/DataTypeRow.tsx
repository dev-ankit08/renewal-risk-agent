// One data type in a section: which connected system and which of its objects it
// is read from, and the field that ties a record to its customer. Lock it, then map.
import { useEffect, useState } from "react";
import { api, Connected, DataTypeRow as Row, ObjectField, SaveResult, SystemObject } from "../../api";
import { go, label, Notice } from "../../ui";

const CUSTOMER_LIST = new Set(["account", "customer"]);
const LIKELY_LINK = /account|customer|client|company|org|tenant/i;

interface Props { row: Row; connected: Connected[]; onSaved: (r: SaveResult) => void }

export default function DataTypeRow({ row, connected, onSaved }: Props) {
  const pickable = connected.filter((c) => c.kind);
  const [editing, setEditing] = useState(!row.locked);
  const [source, setSource] = useState(pickable.some((c) => c.name === row.source) ? row.source! : pickable[0]?.name ?? "");
  const [object, setObject] = useState(row.object ?? "");
  const [link, setLink] = useState(row.link ?? "");
  const [objects, setObjects] = useState<SystemObject[] | null>(null);
  const [links, setLinks] = useState(true);
  const [fields, setFields] = useState<ObjectField[]>([]);
  const [error, setError] = useState("");
  const needsLink = links && !CUSTOMER_LIST.has(row.entity);

  useEffect(() => { // the chosen system's objects (cached on the server per connection)
    if (!editing || !source) return;
    setObjects(null); setError("");
    api.get<{ objects: SystemObject[]; links: boolean }>(`/sources/${source}/objects`)
      .then((r) => { setObjects(r.objects); setLinks(r.links); if (!r.objects.some((o) => o.name === object)) setObject(""); })
      .catch((e: Error) => { setObjects([]); setError(e.message); });
  }, [editing, source]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { // the object's fields, to pick the one that links a record to its customer
    if (!editing || !object || !needsLink) return;
    api.get<{ fields: ObjectField[] }>(`/sources/${source}/objects/${encodeURIComponent(object)}/fields`)
      .then((r) => {
        setFields(r.fields);
        if (!r.fields.some((f) => f.name === link)) {
          const candidates = r.fields.filter((f) => f.link);
          setLink((candidates.find((f) => LIKELY_LINK.test(f.name)) ?? candidates[0] ?? r.fields[0])?.name ?? "");
        }
      })
      .catch((e: Error) => setError(e.message));
  }, [editing, source, object, needsLink]); // eslint-disable-line react-hooks/exhaustive-deps

  const lock = () =>
    api.put<SaveResult>(`/assign/${row.entity}`, { source, object, link: needsLink ? link : null })
      .then((r) => { onSaved(r); go(`mapping/${source}/${row.entity}`); })
      .catch((e: Error) => setError(e.message));

  if (!editing) {
    return (
      <div className="row dtrow">
        <strong className="dtname">{label(row.entity)}</strong>
        <span><code>{row.object}</code> in <strong>{row.source}</strong>{row.link && <span className="muted"> · linked by {row.link}</span>} 🔒</span>
        {row.missing?.length ? <span className="badge warn" title={`Required fields still to map: ${row.missing.join(", ")}`}>not mapped</span>
          : <span className="badge ok">mapped</span>}
        <span className="spacer" />
        <button onClick={() => go(`mapping/${row.source}/${row.entity}`)}>Map fields</button>
        <button onClick={() => setEditing(true)}>Change</button>
      </div>
    );
  }
  const sorted = [...fields].sort((a, b) => Number(b.link) - Number(a.link));
  return (
    <div className="dtrow">
      <div className="row">
        <strong className="dtname">{label(row.entity)}</strong>
        {pickable.length === 0 ? <span className="muted">Connect a system above first.</span> : (
          <>
            <span className="muted">from</span>
            <select value={source} onChange={(e) => setSource(e.target.value)} aria-label={`${row.entity} system`}>
              {pickable.map((c) => <option key={c.name} value={c.name}>{c.name} ({c.label})</option>)}
            </select>
            <span className="muted">object</span>
            {objects === null ? <span className="muted small">loading objects…</span> : (
              <>
                <input list={`objects-${row.entity}`} value={object} onChange={(e) => setObject(e.target.value)}
                  placeholder="choose or type…" aria-label={`${row.entity} object`} />
                <datalist id={`objects-${row.entity}`}>
                  {objects.map((o) => <option key={o.name} value={o.name}>{o.label}</option>)}
                </datalist>
              </>
            )}
            {needsLink && object && (
              <>
                <span className="muted">linked by</span>
                <select value={link} onChange={(e) => setLink(e.target.value)} aria-label={`${row.entity} link field`}>
                  {sorted.map((f) => <option key={f.name} value={f.name}>{f.label === f.name ? f.name : `${f.label} (${f.name})`}</option>)}
                </select>
              </>
            )}
            <span className="spacer" />
            <button className="primary" disabled={!object || (needsLink && !link)} onClick={lock}>Lock & map fields</button>
            {row.locked && <button onClick={() => setEditing(false)}>Cancel</button>}
          </>
        )}
      </div>
      {!row.locked && row.object && <div className="muted small">Now read from {row.object} in {row.source} (default) — lock to confirm or change it.</div>}
      <Notice kind="bad">{error}</Notice>
    </div>
  );
}
