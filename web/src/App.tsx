import { lazy, Suspense, useEffect, useState } from "react";
import { api, ApiError, Status } from "./api";
import { switchWorkspace, useChecklist } from "./Checklist";
import { go, useHashRoute } from "./ui";
import AccessPage from "./pages/Access";
import HealthPage from "./pages/Health";
import MappingPage from "./pages/Mapping";
import MatchingPage from "./pages/Matching";
import SetupPage, { LoginPage } from "./pages/Setup";
import SignalsPage from "./pages/Signals";
import SourcesPage from "./pages/Sources";

const AccuracyPage = lazy(() => import("./pages/Accuracy"));
const AgentsPage = lazy(() => import("./pages/Agents"));

const NAV = [
  ["sources", "Data sources"],
  ["matching", "Account matching"],
  ["signals", "Signals & playbooks"],
  ["agents", "AI agents"],
  ["access", "Access & Slack"],
  ["health", "Health & feedback"],
  ["accuracy", "Accuracy"],
];

function SetupProgress() {
  const list = useChecklist();
  if (!list.data || list.data.done === list.data.total) return null;
  return (
    <a href="#/sources" className="progress" title="What is left to set up">
      Setup {list.data.done}/{list.data.total}
      <div className="meter"><span style={{ width: `${(100 * list.data.done) / list.data.total}%` }} /></div>
    </a>
  );
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [admin, setAdmin] = useState<string | null | undefined>(undefined);
  const [route, params] = useHashRoute();
  const page = route.split("/")[0];

  const loadStatus = () => api.get<Status>("/status").then((s) => { setStatus(s); return s; });
  const refresh = () =>
    loadStatus().then((s) => {
      if (!s.needs_setup) {
        api.get<{ email: string }>("/me")
          .then((m) => setAdmin(m.email))
          .catch((e: ApiError) => setAdmin(e.status === 401 ? null : undefined));
      }
    });
  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => { // the sidebar follows the setup (e.g. Account matching appears once it is needed)
    if (status) loadStatus();
  }, [page]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!status) return <div className="center muted">Loading…</div>;
  if (status.needs_setup) return <SetupPage status={status} onDone={refresh} />;
  if (admin === undefined) return <div className="center muted">Loading…</div>;
  if (admin === null) return <LoginPage onDone={refresh} />;

  const packLabel = status.packs.find((p) => p.name === status.pack)?.label ?? status.pack;
  // Account matching only when a second system keeps its own customer list (or you are on it)
  const nav = NAV.filter(([key]) => key !== "matching" || status.matching_needed || page === "matching");
  return (
    <div className="layout">
      <aside className="side">
        <h1>Renewal Risk</h1>
        <div className="pack">{packLabel}</div>
        <SetupProgress />
        <nav>
          {nav.map(([key, text], i) => (
            <a key={key} href={`#/${key}`} className={page === key || (key === "sources" && page === "mapping") ? "active" : ""}>
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
          {!status.model_configured && <p><span className="badge warn">Rules only — no AI key</span></p>}
        </div>
      </aside>
      <main>
        {status.workspace.active === "demo" && (
          <div className="notice warn row">
            <span><strong>Demo workspace</strong> — sample data. Your real setup is untouched.</span>
            <span className="spacer" />
            <button onClick={() => switchWorkspace("home")}>Switch back to your real setup</button>
          </div>
        )}
        {page === "sources" && <SourcesPage params={params} status={status} onChange={loadStatus} />}
        {page === "mapping" && <MappingPage route={route} />}
        {page === "matching" && <MatchingPage />}
        {page === "signals" && <SignalsPage params={params} />}
        {page === "access" && <AccessPage />}
        {page === "health" && <HealthPage />}
        {page === "accuracy" && <Suspense fallback={null}><AccuracyPage /></Suspense>}
        {page === "agents" && <Suspense fallback={null}><AgentsPage /></Suspense>}
        {!NAV.some(([k]) => k === page) && page !== "mapping" && (
          <p>
            Unknown page. <a href="#/sources" onClick={() => go("sources")}>Go to data sources</a>
          </p>
        )}
      </main>
    </div>
  );
}
