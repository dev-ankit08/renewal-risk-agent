import { ReactNode, useEffect, useId, useState } from "react";

// An ⓘ that shows its explanation on hover, keyboard focus or tap (Esc closes) — the one
// place details and descriptions live, so screens stay short. `wide` for longer text.
export function Tip({ children, label = "More information", wide = false }: { children: ReactNode; label?: string; wide?: boolean }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  return (
    <span className="tip" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" className="tip-btn" aria-label={label} aria-expanded={open} aria-describedby={open ? id : undefined}
        onClick={(e) => { e.preventDefault(); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}>i</button>
      {open && <span role="tooltip" id={id} className={`tip-body${wide ? " wide" : ""}`}>{children}</span>}
    </span>
  );
}

// A page title with one short line under it; everything else goes in its tip.
export function PageHead({ title, sub, help, children }: { title: string; sub?: ReactNode; help?: ReactNode; children?: ReactNode }) {
  return (
    <header className="pagehead">
      <div>
        <div className="titlerow"><h2>{title}</h2>{help && <Tip label={`About ${title}`} wide>{help}</Tip>}</div>
        {sub && <p className="sub">{sub}</p>}
      </div>
      {children && <div className="actions">{children}</div>}
    </header>
  );
}

// A form label with its explanation in a tip.
export function Field({ label, tip, children }: { label: ReactNode; tip?: ReactNode; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}{tip && <Tip>{tip}</Tip>}</label>
      {children}
    </div>
  );
}

export function useHashRoute(): [string, URLSearchParams] {
  const read = () => {
    const raw = window.location.hash.replace(/^#\/?/, "");
    const [path, query] = raw.split("?");
    return [path || "sources", new URLSearchParams(query || "")] as [string, URLSearchParams];
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function go(path: string) {
  window.location.hash = `#/${path}`;
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label={title}>
        <div className="row">
          <h3 style={{ margin: 0 }}>{title}</h3>
          <span className="spacer" />
          <button onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Notice({ kind, children }: { kind?: "ok" | "bad" | "warn"; children: ReactNode }) {
  if (!children) return null;
  return <div className={`notice ${kind ?? ""}`}>{children}</div>;
}

export function SaveFeedback({ result }: { result: { errors: string[]; warnings: string[]; note?: string } | null }) {
  if (!result) return null;
  return (
    <>
      {result.errors.length > 0 && (
        <Notice kind="warn">
          Saved. Still to do:
          <ul>{result.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </Notice>
      )}
      {result.errors.length === 0 && <Notice kind="ok">Saved.{result.note ? ` ${result.note}` : ""}</Notice>}
    </>
  );
}

export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const reload = () => {
    setLoading(true);
    setError(null);
    loader()
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, deps);
  return { data, error, loading, reload, setData };
}

export const label = (entity: string) => entity.replace(/_/g, " ");
