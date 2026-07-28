import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../layout/AuthProvider";
import { Card, Button, Icon } from "../components/ui";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/SsoCallback.jsx
 *
 * Phase E13 — the OIDC redirect target. The identity provider sends the
 * operator back here with either `?code=…&state=…` or `?error=…`; this
 * page hands that query string to AuthProvider.completeSsoLogin(), which
 * performs the server-side code exchange and installs the resulting token
 * through the same path every other sign-in uses.
 *
 * The page renders exactly three honest states — exchanging, failed (with
 * the real reason, and a way out), or nothing at all because it already
 * navigated on success. It never shows a spinner that cannot end: a
 * failure always resolves to a message and a link back to /login.
 * ────────────────────────────────────────────────────────────────────────── */

export default function SsoCallback() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [error, setError] = useState(null);
  // React 18 StrictMode double-invokes effects in development; the
  // authorization code is single-use, so a second exchange would fail
  // against the IdP and overwrite a successful sign-in with an error.
  const startedRef = useRef(false);

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;

    let cancelled = false;
    (async () => {
      const result = await auth.completeSsoLogin(location.search);
      if (cancelled) return;
      if (result.ok) {
        navigate("/", { replace: true });
      } else {
        setError(result.error);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: "var(--bg)", padding: "var(--sp-6)",
    }}>
      <Card style={{ width: "min(420px, 100%)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--sp-2)", marginBottom: "var(--sp-4)" }}>
          <span style={{ fontSize: "1.5rem" }}>⬡</span>
          <div>
            <div style={{ fontFamily: "var(--font-display)", fontSize: "var(--fs-lg)", color: "var(--text)" }}>
              AEAM Console
            </div>
            <div style={{ fontSize: "var(--fs-xs)", color: "var(--muted)" }}>
              {error ? "Single sign-on failed" : "Completing single sign-on…"}
            </div>
          </div>
        </div>

        {error ? (
          <>
            <div role="alert" style={{
              display: "flex", alignItems: "flex-start", gap: "var(--sp-2)", color: "var(--err)",
              fontSize: "var(--fs-xs)",
            }}>
              <Icon name="alert" size={13} /> <span>{error}</span>
            </div>
            <div style={{ marginTop: "var(--sp-4)" }}>
              <Button
                variant="primary"
                style={{ width: "100%" }}
                onClick={() => navigate("/login", { replace: true })}
              >
                Back to sign in
              </Button>
            </div>
          </>
        ) : (
          <p style={{ fontSize: "var(--fs-xs)", color: "var(--muted)" }}>
            Exchanging the authorization code issued by your identity provider.
          </p>
        )}
      </Card>
    </div>
  );
}
