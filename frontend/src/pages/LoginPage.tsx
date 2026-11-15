import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { ApiError } from "../api";

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(username, password);
      navigate("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center">
      <div className="card w-full max-w-sm p-8">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-accent-deep text-xl font-bold text-ink-950">
            A
          </div>
          <h1 className="text-xl font-semibold text-ink-100">ArcusClips</h1>
          <p className="mt-1 text-sm text-ink-500">
            Local-first AI video clipping studio
          </p>
        </div>
        <form onSubmit={submit} className="space-y-4">
          <label className="block">
            <span className="label">Username</span>
            <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <label className="block">
            <span className="label">Password</span>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="changeme"
            />
          </label>
          {error && <div className="text-sm text-danger">{error}</div>}
          <button className="btn-primary w-full" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="mt-4 text-center text-xs text-ink-500">
          Default local account: admin / changeme — change it in deployment.
        </p>
      </div>
    </div>
  );
}
