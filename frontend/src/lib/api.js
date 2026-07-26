/*
 * frontend/src/lib/api.js
 *
 * Shared API fetch helpers (Phase E6 — Scale Contracts).
 *
 * Before E6 every page defined its own inline `fetchJSON`. This module is
 * the single shared helper so paged fetching and the X-Total-Count contract
 * are implemented once. The backend list endpoints are backward-compatible:
 * a parameter-less call still returns the full array, so `fetchJSON` alone
 * is a drop-in for the old inline helpers. `fetchPage` is the new,
 * bounded-consumption path.
 */

/** Fetch JSON, throwing on non-2xx. Drop-in for the old inline helpers. */
export async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/* ──────────────────────────────────────────────────────────────────────────
 * Phase E10 — session/bearer-token plumbing.
 *
 * Every page in this codebase calls the backend with either a bare `fetch()`
 * or the two helpers above, always with a same-origin relative path
 * (`/api/...`, `/health`, `/metrics`). Rather than touching every call site
 * to attach `Authorization: Bearer <token>`, this module wraps the global
 * `fetch` exactly once (module import is a singleton) so every existing
 * call site gets the header for free, and gets routed through one place
 * when the backend responds 401 (token missing/expired/invalid) so the
 * session layer can react honestly instead of a page silently rendering
 * empty data. AuthProvider (layout/AuthProvider.jsx) is the only caller of
 * `setAuthToken` / `setUnauthorizedHandler`.
 * ────────────────────────────────────────────────────────────────────────── */

let _authToken = null;
let _onUnauthorized = null;

export function setAuthToken(token) {
  _authToken = token || null;
}

export function getAuthToken() {
  return _authToken;
}

/** Registered once by AuthProvider; called whenever a same-origin API call gets a 401. */
export function setUnauthorizedHandler(fn) {
  _onUnauthorized = typeof fn === "function" ? fn : null;
}

function _isSameOriginApiUrl(input) {
  const url = typeof input === "string" ? input : input?.url || "";
  return url.startsWith("/api/") || url.startsWith("/health") || url.startsWith("/metrics");
}

if (typeof window !== "undefined" && !window.__aeamFetchPatched) {
  window.__aeamFetchPatched = true;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const tagged = _isSameOriginApiUrl(input);
    let finalInit = init;
    if (tagged && _authToken) {
      finalInit = {
        ...init,
        headers: { ...(init.headers || {}), Authorization: `Bearer ${_authToken}` },
      };
    }
    const res = await nativeFetch(input, finalInit);
    if (tagged && res.status === 401 && _onUnauthorized) {
      _onUnauthorized();
    }
    return res;
  };
}

/**
 * Fetch a bounded page of a list endpoint.
 *
 * Appends `limit`/`offset` (plus any extra query params) and reads the
 * `X-Total-Count` response header the E6 endpoints emit, so a caller can
 * page without a second "count" request.
 *
 * @param {string} path      Endpoint path, e.g. "/api/v1/incidents/".
 * @param {object} opts
 * @param {number} opts.limit   Page size (default 100).
 * @param {number} opts.offset  Rows to skip (default 0).
 * @param {object} opts.params  Extra query params (e.g. { severity: "HIGH" }).
 * @returns {Promise<{ items: any[], total: number|null, limit: number, offset: number, hasMore: boolean }>}
 */
export async function fetchPage(path, { limit = 100, offset = 0, params = {} } = {}) {
  const qs = new URLSearchParams();
  qs.set("limit", String(limit));
  qs.set("offset", String(offset));
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }

  const res = await fetch(`${path}?${qs.toString()}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const items = await res.json();
  const totalHeader = res.headers.get("X-Total-Count");
  const total = totalHeader != null && totalHeader !== "" ? Number(totalHeader) : null;
  const count = Array.isArray(items) ? items.length : 0;

  // hasMore is derived from the total when the header is present; otherwise
  // fall back to "a full page came back, so there may be more".
  const hasMore = total != null ? offset + count < total : count >= limit;

  return { items: Array.isArray(items) ? items : [], total, limit, offset, hasMore };
}
