import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../layout/AuthProvider";
import { Card, Button, Icon } from "../components/ui";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Login.jsx
 *
 * Phase E10 console login surface. AEAM validates enterprise-issued tokens
 * rather than acting as an identity provider, so this page's only real job
 * is: accept a bearer token (obtained from whatever issues them for this
 * deployment) and hand it to AuthProvider. Full OIDC/SSO redirect flow is
 * Phase E13; this page is the landing zone it will attach a "Sign in with
 * SSO" button to, next to the same paste-token fallback.
 *
 * In a development posture AuthProvider auto-acquires a token before this
 * page would ever be reached (RequireAuth redirects here only once booting
 * is false and no token was acquired), so in practice only staging/
 * production operators see this form.
 * ────────────────────────────────────────────────────────────────────────── */

export default function Login() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [tokenInput, setTokenInput] = useState("");
  const [error, setError] = useState(null);

  function handleSubmit(e) {
    e.preventDefault();
    const result = auth.login(tokenInput);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    const dest = location.state?.from?.pathname || "/";
    navigate(dest, { replace: true });
  }

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: "var(--bg)", padding: "var(--sp-6)",
    }}>
      <Card style={{ width: "min(420px, 100%)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--sp-2)", marginBottom: "var(--sp-4)" }}>
          <span style={{ fontSize: "1.5rem" }}>⬡</span>
          <div>
            <div style={{ fontFamily: "var(--font-display)", fontSize: "var(--fs-lg)", color: "var(--text)" }}>AEAM Console</div>
            <div style={{ fontSize: "var(--fs-xs)", color: "var(--muted)" }}>Sign in to continue</div>
          </div>
        </div>

        <form onSubmit={handleSubmit}>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            <span style={{
              fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)",
            }}>Bearer token</span>
            <textarea
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder="Paste the JWT issued to you by your identity provider…"
              rows={5}
              aria-label="Bearer token"
              style={{
                width: "100%", resize: "vertical", fontFamily: "var(--font-mono)",
                fontSize: "var(--fs-xs)", background: "var(--surface-2)", color: "var(--text)",
                border: "1px solid var(--border)", borderRadius: "var(--r-md)", padding: "var(--sp-3)",
              }}
            />
          </label>

          {error && (
            <div role="alert" style={{
              display: "flex", alignItems: "center", gap: "var(--sp-2)", color: "var(--err)",
              fontSize: "var(--fs-xs)", marginTop: "var(--sp-2)",
            }}>
              <Icon name="alert" size={13} /> {error}
            </div>
          )}

          <div style={{ marginTop: "var(--sp-4)" }}>
            <Button type="submit" variant="primary" style={{ width: "100%" }}>Sign in</Button>
          </div>
        </form>

        <p style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", marginTop: "var(--sp-4)" }}>
          AEAM validates tokens issued by your organization's identity provider — it does
          not issue credentials itself. Federated single sign-on lands in a future phase.
        </p>
      </Card>
    </div>
  );
}
