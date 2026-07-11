import { createContext, useContext, useEffect, useState, useCallback, useRef } from "react";
import { useToast } from "./ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/HealthProvider.jsx
 * Polls the two EXISTING backend endpoints the shell is allowed to read and
 * shares the result with the TopBar (system pill) and StatusBar (dependency
 * chips) — one poll, two consumers, no duplication.
 *
 *   /health                 → { status, checks:{ database, redis, queue } }
 *   /api/v1/system/status   → { status, active_incidents, agents_active, ... }
 *
 * NOTE: Qdrant and LLM are intentionally reported as "unknown". No proxied
 * endpoint exposes their health today; the shell shows honest "n/a" chips
 * rather than fabricating green. Wiring real Qdrant/LLM probes is a backend
 * task deferred to Phase A3 (see deliverables). No backend API is changed.
 * ────────────────────────────────────────────────────────────────────────── */

const HealthContext = createContext(null);
const POLL_MS = 15000;

function normalize(raw) {
  // /health "checks" values look like "ok" | "error: ..." | "disabled ..."
  const v = String(raw ?? "").toLowerCase();
  if (v.startsWith("ok")) return "ok";
  if (v.startsWith("disabled")) return "disabled";
  if (v === "healthy") return "ok";
  if (v === "degraded") return "degraded";
  if (v.startsWith("error")) return "error";
  return "unknown";
}

export function HealthProvider({ children }) {
  const toast = useToast();
  const [health, setHealth] = useState(null);   // /health payload
  const [status, setStatus] = useState(null);   // /system/status payload
  const [reachable, setReachable] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const wasDownRef = useRef(false);

  const poll = useCallback(async () => {
    let ok = true;
    try {
      const [h, s] = await Promise.allSettled([
        fetch("/health").then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
        fetch("/api/v1/system/status").then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
      ]);
      if (h.status === "fulfilled") setHealth(h.value); else ok = false;
      if (s.status === "fulfilled") setStatus(s.value); else ok = false;
    } catch {
      ok = false;
    }
    setReachable(ok);
    setUpdatedAt(new Date());

    // Real usage of the toast system: surface transitions, not every tick.
    if (!ok && !wasDownRef.current) {
      wasDownRef.current = true;
      toast.error("Backend unreachable", "The AEAM API is not responding to health checks.");
    } else if (ok && wasDownRef.current) {
      wasDownRef.current = false;
      toast.success("Backend recovered", "Health checks are passing again.");
    }
  }, [toast]);

  useEffect(() => {
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => clearInterval(id);
  }, [poll]);

  const checks = health?.checks || {};
  const value = {
    reachable,
    updatedAt,
    overall: reachable ? normalize(health?.status || status?.status) : "error",
    agentsActive: status?.agents_active ?? null,
    activeIncidents: status?.active_incidents ?? null,
    lastEventTime: status?.last_event_time ?? null,
    deps: {
      backend:  reachable ? normalize(health?.status) : "error",
      database: reachable ? normalize(checks.database) : "unknown",
      redis:    reachable ? normalize(checks.redis) : "unknown",
      queue:    reachable ? normalize(checks.queue) : "unknown",
      qdrant:   "unknown", // not reported by /health — honest placeholder
      llm:      "unknown", // not reported by /health — honest placeholder
    },
    refresh: poll,
  };

  return <HealthContext.Provider value={value}>{children}</HealthContext.Provider>;
}

export function useHealth() {
  return useContext(HealthContext) || {
    reachable: false, deps: {}, overall: "unknown", refresh() {},
  };
}
