import { useEffect, useState } from "react";
import { api, ApiError, Status } from "./api";
import { go, useHashRoute } from "./ui";
import AccessPage from "./pages/Access";
import HealthPage from "./pages/Health";
import MappingPage from "./pages/Mapping";
import MatchingPage from "./pages/Matching";
import SetupPage, { LoginPage } from "./pages/Setup";
import SignalsPage from "./pages/Signals";
import SourcesPage from "./pages/Sources";

const NAV = [
  ["sources", "Data sources"],
  ["mapping", "Field mapping"],
  ["matching", "Account matching"],
  ["signals", "Signals & playbooks"],
  ["access", "Access & Slack"],
  ["health", "Health & feedback"],
];

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [admin, setAdmin] = useState<string | null | undefined>(undefined);
  const [route, params] = useHashRoute();

  const refresh = () =>
    api.get<Status>("/status").then((s) => {
      setStatus(s);
      if (!s.needs_setup) {
        api.get<{ email: string }>("/me")
          .then((m) => setAdmin(m.email))
          .catch((e: ApiError) => setAdmin(e.status === 401 ? null : undefined));
      }
    });
  useEffect(() => {
    refresh();
  }, []);

  if (!status) return <div className="center muted">Loading…</div>;
  if (status.needs_setup) return <SetupPage status={status} onDone={refresh} />;
  if (admin === undefined) return <div className="center muted">Loading…</div>;
  if (admin === null) return <LoginPage onDone={refresh} />;

  const page = route.split("/")[0];
  const packLabel = status.packs.find((p) => p.name === status.pack)?.label ?? status.pack;
  return (
    <div className="layout">
      <aside className="side">
        <h1>Renewal Risk</h1>
        <div className="pack">{packLabel}</div>
        <nav>
          {NAV.map(([key, text], i) => (
            <a key={key} href={`#/${key}`} className={page === key ? "active" : ""}>
              <span className="step">{i + 1}</span>
              {text}
            </a>
          ))}
        </nav>
        <div className="foot">
          {admin}
          <br />
          <a href="#/sources" onClick={(e) => { e.preventDefault(); api.post("/logout").then(() => setAdmin(null)); }}>Sign out</a>
          {" · "}
          <a href="/" target="_blank" rel="noreferrer">Ask page</a>
          {!status.model_configured && <p className="badge warn">No ANTHROPIC_API_KEY: AI mapping uses rules only</p>}
        </div>
      </aside>
      <main>
        {page === "sources" && <SourcesPage params={params} status={status} onPackChange={refresh} />}
        {page === "mapping" && <MappingPage route={route} />}
        {page === "matching" && <MatchingPage />}
        {page === "signals" && <SignalsPage />}
        {page === "access" && <AccessPage />}
        {page === "health" && <HealthPage />}
        {!NAV.some(([k]) => k === page) && (
          <p>
            Unknown page. <a href="#/sources" onClick={() => go("sources")}>Go to data sources</a>
          </p>
        )}
      </main>
    </div>
  );
}
