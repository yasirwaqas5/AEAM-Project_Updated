import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../layout/AuthProvider";
import { Card, Button, Icon } from "../components/ui";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Login.jsx
 *
 * Phase E10 console login surface, completed by Phase E13's SSO redirect.
 * AEAM validates enterprise-issued tokens rather than acting as an identity
 * provider, so this page offers exactly two ways to obtain one:
 *
 *   1. "Sign in with SSO" — starts the OIDC authorization-code + PKCE
 *      redirect, shown only when the deployment actually federates
 *      identity (GET /api/v1/auth/sso/config says so). When SSO is off,
 *      the button is absent and the reason is stated rather than a
 *      dead control being rendered (EXPL-5: nothing renders as real that
 *      isn't).
 *   2. Pasting a bearer token issued by the organization's IdP — the
 *      pre-E13 path, kept unchanged as the fallback for deployments
 *      without federation and as the documented rollback if SSO
 *      configuration is reverted.
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
  const [redirecting, setRedirecting] = useState(false);

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

  async function handleSsoSignIn() {
    setError(null);
    setRedirecting(true);
    const result = await auth.loginWithSso();
    if (!result.ok) {
      setError(result.error);
      setRedirecting(false);
    }
    // On success the browser is already navigating to the IdP; leaving
    // `redirecting` true keeps the button disabled until it does.
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

        {auth.ssoEnabled && (
          <div style={{ marginBottom: "var(--sp-4)" }}>
            <Button
              variant="primary"
              style={{ width: "100%" }}
              onClick={handleSsoSignIn}
              disabled={redirecting}
            >
              {redirecting ? "Redirecting to your identity provider…" : "Sign in with SSO"}
            </Button>
            <div style={{
              display: "flex", alignItems: "center", gap: "var(--sp-2)",
              margin: "var(--sp-4) 0 0", color: "var(--faint)", fontSize: "var(--fs-2xs)",
            }}>
              <span style={{ flex: 1, height: 1, background: "var(--border)" }} />
              or paste a token
              <span style={{ flex: 1, height: 1, background: "var(--border)" }} />
            </div>
          </div>
        )}

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
            <Button
              type="submit"
              variant={auth.ssoEnabled ? "ghost" : "primary"}
              style={{ width: "100%" }}
            >
              Sign in
            </Button>
          </div>
        </form>

        <p style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", marginTop: "var(--sp-4)" }}>
          AEAM validates tokens issued by your organization&rsquo;s identity provider — it does
          not issue credentials itself.
          {auth.sso && !auth.sso.enabled && (
            <> Single sign-on is not available: {auth.sso.reason}</>
          )}
        </p>
      </Card>
    </div>
  );
}
