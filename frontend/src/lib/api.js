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
