import { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { setAuthToken, setUnauthorizedHandler } from "../lib/api";
import { hasPermission } from "../lib/rbac";
import { useToast } from "./ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/AuthProvider.jsx
 *
 * Phase E10 — Enterprise Console session layer.
 * Phase E13 — enterprise SSO federation lands here, as the roadmap said it
 *             would: no second session layer, just a second way to acquire
 *             the token this one already manages.
 *
 * AEAM validates tokens issued elsewhere; it is not an identity provider.
 * This provider's job is narrower and entirely session-side:
 *
 *   - Hold the current bearer token (sessionStorage-backed — cleared when
 *     the tab closes, never persisted across browser restarts).
 *   - Decode its claims client-side for role-aware UI (no crypto
 *     verification happens here; the JWT signature is only ever verified
 *     server-side by SecurityMiddleware — this is a UI-affordance read,
 *     not a security boundary).
 *   - Attach it to every same-origin API call (via lib/api.js's fetch
 *     wrap) and react to server-declared expiry/401s honestly: log out
 *     and prompt re-authentication, never let a page silently render
 *     empty data because its calls started failing.
 *   - In a development posture (ENVIRONMENT=development, where
 *     SecurityMiddleware bypasses every check), auto-acquire a dev token
 *     from the dev-only `/api/v1/auth/dev-token` endpoint so local work
 *     keeps its pre-E10 zero-friction behaviour (Rollback strategy in
 *     ROADMAP.md E10: "Auth UI behind an environment flag preserving
 *     today's open-dev behaviour for local work"). That endpoint itself
 *     404s outside development, so this is a no-op probe everywhere else.
 *   - (E13) Drive the OIDC authorization-code + PKCE redirect when the
 *     deployment federates identity. The browser sends the user to the
 *     IdP; the IdP redirects back to /auth/callback with a code; the code
 *     is exchanged server-side (so a confidential-client secret never
 *     reaches the browser) and the resulting token flows into exactly the
 *     same applyToken() path a pasted token uses. Nothing downstream of
 *     this file knows or cares which way the token arrived.
 *
 * PKCE note: the code verifier is held in sessionStorage for the duration
 * of the redirect — it must survive a full page navigation to the IdP and
 * back, which rules out component state, and it is single-use and
 * tab-scoped, which is exactly what sessionStorage gives. It is cleared
 * the moment the exchange completes, successfully or not.
 * ────────────────────────────────────────────────────────────────────────── */

const AuthContext = createContext(null);
const STORAGE_KEY = "aeam.auth.token";
const PKCE_VERIFIER_KEY = "aeam.auth.pkce_verifier";
const OIDC_STATE_KEY = "aeam.auth.oidc_state";

/** Random URL-safe string for the PKCE verifier and the CSRF `state`. */
function randomUrlSafe(bytes = 32) {
  const raw = new Uint8Array(bytes);
  (globalThis.crypto || {}).getRandomValues?.(raw);
  let out = "";
  for (const byte of raw) out += String.fromCharCode(byte);
  return btoa(out).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** S256 PKCE challenge, or `null` when WebCrypto's digest is unavailable.
 *
 *  Returning null rather than silently downgrading to `plain` is
 *  deliberate: the server advertises S256 and the IdP is registered for
 *  it, so a downgraded challenge would simply be rejected at the token
 *  endpoint — failing here, before the redirect, gives the operator an
 *  actionable message instead of a confusing IdP error page. */
async function pkceChallenge(verifier) {
  const subtle = (globalThis.crypto || {}).subtle;
  if (!subtle?.digest) return null;
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  let binary = "";
  for (const byte of new Uint8Array(digest)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeJwtPayload(token) {
  try {
    const [, payloadB64] = token.split(".");
    if (!payloadB64) return null;
    const normalized = payloadB64.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    const json = decodeURIComponent(
      atob(padded)
        .split("")
        .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
        .join("")
    );
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function isExpired(claims) {
  if (!claims?.exp) return false;
  return Date.now() >= claims.exp * 1000;
}

export function AuthProvider({ children }) {
  const [token, _setToken] = useState(null);
  const [claims, setClaims] = useState(null);
  const [devMode, setDevMode] = useState(false);
  const [booting, setBooting] = useState(true);
  // null while the probe is in flight; then always an object with
  // `enabled` — never left undefined, so the UI can distinguish "still
  // asking" from "asked, and the answer is no, because <reason>".
  const [sso, setSso] = useState(null);
  const expiryTimerRef = useRef(null);
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const applyToken = useCallback((nextToken, { persist = true } = {}) => {
    clearTimeout(expiryTimerRef.current);
    if (!nextToken) {
      _setToken(null);
      setClaims(null);
      setAuthToken(null);
      if (persist) sessionStorage.removeItem(STORAGE_KEY);
      return;
    }
    const decoded = decodeJwtPayload(nextToken);
    if (!decoded || isExpired(decoded)) {
      _setToken(null);
      setClaims(null);
      setAuthToken(null);
      if (persist) sessionStorage.removeItem(STORAGE_KEY);
      return;
    }
    _setToken(nextToken);
    setClaims(decoded);
    setAuthToken(nextToken);
    if (persist) sessionStorage.setItem(STORAGE_KEY, nextToken);

    if (decoded.exp) {
      const msUntilExpiry = decoded.exp * 1000 - Date.now();
      expiryTimerRef.current = setTimeout(() => {
        applyToken(null);
        toast.warning("Session expired", "Please sign in again to continue.");
        navigate("/login", { replace: true });
      }, Math.max(0, msUntilExpiry));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigate, toast]);

  const logout = useCallback(() => {
    applyToken(null);
    setDevMode(false);
    navigate("/login", { replace: true });
  }, [applyToken, navigate]);

  const login = useCallback((rawToken) => {
    const cleaned = (rawToken || "").trim();
    if (!cleaned) return { ok: false, error: "Token is empty." };
    const decoded = decodeJwtPayload(cleaned);
    if (!decoded) return { ok: false, error: "That doesn't look like a valid JWT." };
    if (isExpired(decoded)) return { ok: false, error: "That token has already expired." };
    applyToken(cleaned);
    return { ok: true };
  }, [applyToken]);

  /* ── Phase E13: SSO redirect flow ─────────────────────────────────────
   * loginWithSso() sends the browser to the IdP. completeSsoLogin() is
   * called by the /auth/callback route with the query string the IdP
   * redirected back with. Both report failures as { ok:false, error } so
   * the caller can show a real message rather than a blank screen. */

  const loginWithSso = useCallback(async () => {
    if (!sso?.enabled) {
      return { ok: false, error: sso?.reason || "Single sign-on is not configured for this deployment." };
    }

    const verifier = randomUrlSafe();
    const challenge = await pkceChallenge(verifier);
    if (!challenge) {
      return {
        ok: false,
        error:
          "This browser does not expose WebCrypto (SHA-256), which PKCE requires. " +
          "Serve the console over HTTPS, or sign in with a pasted token.",
      };
    }

    const state = randomUrlSafe(16);
    sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
    sessionStorage.setItem(OIDC_STATE_KEY, state);

    const redirectUri = sso.redirect_uri || `${window.location.origin}/auth/callback`;
    const params = new URLSearchParams({
      response_type: sso.response_type || "code",
      client_id: sso.client_id,
      redirect_uri: redirectUri,
      scope: sso.scopes || "openid profile email",
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
    });

    window.location.assign(`${sso.authorization_endpoint}?${params.toString()}`);
    return { ok: true };
  }, [sso]);

  const completeSsoLogin = useCallback(async (search) => {
    const params = new URLSearchParams(search || "");
    const verifier = sessionStorage.getItem(PKCE_VERIFIER_KEY) || "";
    const expectedState = sessionStorage.getItem(OIDC_STATE_KEY) || "";
    // Single-use: cleared before any early return so a failed attempt can
    // never be replayed with the same verifier.
    sessionStorage.removeItem(PKCE_VERIFIER_KEY);
    sessionStorage.removeItem(OIDC_STATE_KEY);

    const idpError = params.get("error");
    if (idpError) {
      return { ok: false, error: params.get("error_description") || idpError };
    }

    const code = params.get("code");
    if (!code) return { ok: false, error: "The identity provider returned no authorization code." };

    const returnedState = params.get("state") || "";
    if (expectedState && returnedState !== expectedState) {
      return { ok: false, error: "Sign-in state did not match. Start the sign-in again." };
    }

    let body;
    try {
      const res = await fetch("/api/v1/auth/sso/callback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code,
          code_verifier: verifier,
          redirect_uri: sso?.redirect_uri || `${window.location.origin}/auth/callback`,
        }),
      });
      body = await res.json().catch(() => ({}));
      if (!res.ok) {
        return { ok: false, error: body?.detail || `Sign-in failed (HTTP ${res.status}).` };
      }
    } catch {
      return { ok: false, error: "Could not reach AEAM to complete sign-in." };
    }

    if (!body?.access_token) return { ok: false, error: "No access token was issued." };

    const decoded = decodeJwtPayload(body.access_token);
    if (!decoded) return { ok: false, error: "The identity provider issued a token this console cannot read." };
    if (isExpired(decoded)) return { ok: false, error: "The issued token is already expired." };

    applyToken(body.access_token);
    return { ok: true };
  }, [applyToken, sso]);

  // Boot sequence: try dev auto-login first (no-op outside development via
  // the endpoint's own 404 gate), then fall back to a previously pasted
  // token still valid in this tab's sessionStorage. The SSO probe runs in
  // both cases -- a dev-posture session should still be able to show what
  // the deployment's identity configuration is.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const res = await fetch("/api/v1/auth/sso/config");
        const body = res.ok ? await res.json() : null;
        if (!cancelled) {
          setSso(
            body && typeof body.enabled === "boolean"
              ? body
              : { enabled: false, reason: "The SSO configuration endpoint is unavailable." }
          );
        }
      } catch {
        if (!cancelled) {
          setSso({ enabled: false, reason: "AEAM is unreachable; SSO status is unknown." });
        }
      }
    })();

    (async () => {
      try {
        const res = await fetch("/api/v1/auth/dev-token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sub: "dev-user", roles: ["admin"] }),
        });
        if (!cancelled && res.ok) {
          const body = await res.json();
          applyToken(body.access_token, { persist: false });
          setDevMode(true);
          setBooting(false);
          return;
        }
      } catch {
        // Backend unreachable at boot -- fall through to stored-token check;
        // HealthProvider already surfaces "backend unreachable" separately.
      }
      if (cancelled) return;
      const stored = sessionStorage.getItem(STORAGE_KEY);
      if (stored) applyToken(stored);
      setBooting(false);
    })();

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Honest 401 handling: any same-origin API call that comes back 401 means
  // the session is no longer valid server-side (expired/revoked/malformed) --
  // never let a page keep rendering stale/empty data believing it's fine.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (devMode) return; // dev posture bypasses auth entirely; a 401 here can't happen from expiry
      applyToken(null);
      toast.error("Session expired", "Please sign in again to continue.");
      if (location.pathname !== "/login") {
        navigate("/login", { replace: true, state: { from: location } });
      }
    });
    return () => setUnauthorizedHandler(null);
  }, [applyToken, devMode, navigate, location, toast]);

  const value = {
    token,
    claims,
    sub: claims?.sub || null,
    roles: claims?.roles || [],
    isAuthenticated: Boolean(token),
    isDev: devMode,
    booting,
    login,
    logout,
    // Phase E13. `sso` is null until the probe answers, then always an
    // object carrying `enabled` and, when disabled, the honest `reason`.
    sso,
    ssoEnabled: Boolean(sso?.enabled),
    loginWithSso,
    completeSsoLogin,
    hasPermission: (resource, action) => hasPermission(claims?.roles || [], resource, action),
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    // Safe no-op fallback (mirrors useHealth/useToast) so a component never
    // crashes if it somehow renders outside the provider.
    return {
      token: null, claims: null, sub: null, roles: [], isAuthenticated: false,
      isDev: false, booting: false, login: () => ({ ok: false, error: "No AuthProvider." }),
      logout() {},
      sso: null, ssoEnabled: false,
      loginWithSso: async () => ({ ok: false, error: "No AuthProvider." }),
      completeSsoLogin: async () => ({ ok: false, error: "No AuthProvider." }),
      hasPermission: () => false,
    };
  }
  return ctx;
}
