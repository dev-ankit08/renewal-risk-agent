import { FormEvent, useState } from "react";
import { api, Status } from "../api";
import { Notice } from "../ui";

export default function SetupPage({ status, onDone }: { status: Status; onDone: () => void }) {
  const [form, setForm] = useState({ setup_token: "", email: "", password: "", pack: status.packs[0]?.name ?? "saas" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    api.post("/setup", form).then(onDone).catch((err: Error) => setError(err.message)).finally(() => setBusy(false));
  };

  return (
    <form className="center card" onSubmit={submit}>
      <h2>Set up Renewal Risk</h2>
      <p className="muted">Create the first admin account and choose how your business renews.</p>
      <label>What kind of business is this?</label>
      {status.packs.map((p) => (
        <label key={p.name} className="card" style={{ cursor: "pointer", fontWeight: 400, margin: "6px 0" }}>
          <input type="radio" name="pack" value={p.name} checked={form.pack === p.name} onChange={set("pack")} /> <strong>{p.label}</strong>
          <div className="muted small">{p.description}</div>
        </label>
      ))}
      <label>Setup token <span className="hint">— printed in the server console on start</span></label>
      <input value={form.setup_token} onChange={set("setup_token")} required autoComplete="off" />
      <label>Your email</label>
      <input type="email" value={form.email} onChange={set("email")} required autoComplete="username" />
      <label>Password <span className="hint">— at least 10 characters</span></label>
      <input type="password" value={form.password} onChange={set("password")} minLength={10} required autoComplete="new-password" />
      <Notice kind="bad">{error}</Notice>
      <p>
        <button className="primary" disabled={busy}>Create admin & continue</button>
      </p>
    </form>
  );
}

export function LoginPage({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    api.post("/login", { email, password }).then(onDone).catch((err: Error) => setError(err.message));
  };
  return (
    <form className="center card" onSubmit={submit}>
      <h2>Sign in</h2>
      <label>Email</label>
      <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" />
      <label>Password</label>
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
      <Notice kind="bad">{error}</Notice>
      <p>
        <button className="primary">Sign in</button>
      </p>
    </form>
  );
}
