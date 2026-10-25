// Upload a CSV / Excel export as a source for one data type, then map its fields.
import { ReactNode, useState } from "react";
import { api, Category, SaveResult } from "../../api";
import { go, label, Modal, Notice } from "../../ui";

interface Props {
  category: Category;
  picker: ReactNode; // the "System" selector of the dialog that opened this one
  name: string;
  setName: (n: string) => void;
  onClose: () => void;
  onSaved: (r: SaveResult, name: string, oauth: boolean) => void;
}

export default function CsvModal(props: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const [entity, setEntity] = useState(props.category.entities[0]);
  const [match, setMatch] = useState("");
  const [testData, setTestData] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const upload = async () => {
    if (!file) return;
    setError(null);
    try {
      await api.put(`/sources/${props.name}`, { type: "csv", test_data: testData });
      const form = new FormData();
      form.append("file", file);
      const r = await api.post<{ file: string; columns: string[] }>(`/sources/${props.name}/upload`, form);
      setUploaded(r.file);
      setColumns(r.columns);
      setMatch(r.columns.find((c) => /account|customer|client/i.test(c)) ?? "");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const finish = () =>
    api.put<SaveResult>(`/sources/${props.name}/csv-entity`, { entity, file: uploaded, match: match || null })
      .then((r) => { props.onSaved(r, "", false); go(`mapping/${props.name}/${entity}`); })
      .catch((e: Error) => setError(e.message));

  return (
    <Modal title={`Upload: ${props.category.label}`} onClose={props.onClose}>
      {props.picker}
      <label>Source name</label>
      <input value={props.name} onChange={(e) => props.setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} />
      <label>File (.csv or .xlsx)</label>
      <input type="file" accept=".csv,.xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      <label className="inline"><input type="checkbox" checked={testData} onChange={(e) => setTestData(e.target.checked)} /> Synthetic test data only</label>
      <p><button onClick={upload} disabled={!file || !props.name}>Upload</button></p>
      {uploaded && (
        <>
          <label>What is in this file?</label>
          <select value={entity} onChange={(e) => setEntity(e.target.value)}>
            {props.category.entities.map((e) => <option key={e} value={e}>{label(e)}</option>)}
          </select>
          <label>Which column holds the customer / account id? <span className="hint">— leave empty for the customer list itself</span></label>
          <select value={match} onChange={(e) => setMatch(e.target.value)}>
            <option value="">(none)</option>
            {columns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <div className="row" style={{ marginTop: 12 }}>
            <span className="spacer" />
            <button className="primary" onClick={finish}>Continue to field mapping</button>
          </div>
        </>
      )}
      <Notice kind="bad">{error}</Notice>
    </Modal>
  );
}
