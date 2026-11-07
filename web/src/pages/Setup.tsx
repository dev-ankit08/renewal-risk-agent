import { FormEvent, useState } from "react";
import { api, Status } from "../api";
import { Field, Notice, Tip } from "../ui";

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
      <p className="sub">Create the first admin account and choose how your business renews.</p>
      <label>What kind of business is this?</label>
      {status.packs.map((p) => (
        <label key={p.name} className="inline" style={{ cursor: "pointer", display: "flex" }}>
          <input type="radio" name="pack" value={p.name} checked={form.pack === p.name} onChange={set("pack")} /> <strong>{p.label}</strong>
          {p.description && <Tip>{p.description}</Tip>}
        </label>
      ))}
      <Field label="Setup token" tip="Printed in the server console when the service starts.">
        <input value={form.setup_token} onChange={set("setup_token")} required autoComplete="off" />
      </Field>
      <Field label="Your email">
        <input type="email" value={form.email} onChange={set("email")} required autoComplete="username" />
      </Field>
      <Field label="Password" tip="At least 10 characters.">
        <input type="password" value={form.password} onChange={set("password")} minLength={10} required autoComplete="new-password" />
      </Field>
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
