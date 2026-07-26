import { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { setAuthToken, setUnauthorizedHandler } from "../lib/api";
import { hasPermission } from "../lib/rbac";
import { useToast } from "./ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/AuthProvider.jsx
 *
 * Phase E10 — Enterprise Console session layer.
 *
 * AEAM validates tokens issued elsewhere; it is not an identity provider
 * (real OIDC/SSO federation is Phase E13's job, landing on this same
 * session layer per the roadmap). This provider's job is narrower and
 * entirely session-side:
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
 * ────────────────────────────────────────────────────────────────────────── */

const AuthContext = createContext(null);
const STORAGE_KEY = "aeam.auth.token";

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

  // Boot sequence: try dev auto-login first (no-op outside development via
  // the endpoint's own 404 gate), then fall back to a previously pasted
  // token still valid in this tab's sessionStorage.
  useEffect(() => {
    let cancelled = false;
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
      logout() {}, hasPermission: () => false,
    };
  }
  return ctx;
}
