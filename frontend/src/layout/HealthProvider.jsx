import { createContext, useContext, useEffect, useState, useCallback, useRef } from "react";
import { useToast } from "./ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/HealthProvider.jsx
 * Polls the two EXISTING backend endpoints the shell is allowed to read and
 * shares the result with the TopBar (system pill) and StatusBar (dependency
 * chips) — one poll, two consumers, no duplication.
 *
 *   /health                 → { status, checks:{ database, redis, queue,
 *                                monitor_agent, ingestion_worker, bm25_index } }
 *   /api/v1/system/status   → { status, active_incidents, agents_active, ... }
 *
 * Phase E7: monitor_agent / ingestion_worker are real heartbeat-backed
 * supervision (OBS-3/4) — a "stale" value means the worker's thread has
 * stopped updating its heartbeat (dead or wedged), surfaced as "degraded".
 * bm25_index is informational lexical-index freshness (RAG-6); it never
 * flips overall health.
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
  // Phase E7 adds "stale (...)" (heartbeat/BM25 supervision) and
  // "starting (...)" (thread alive, no heartbeat recorded yet) forms.
  const v = String(raw ?? "").toLowerCase();
  if (v.startsWith("ok")) return "ok";
  if (v.startsWith("disabled") || v.startsWith("not started")) return "disabled";
  if (v.startsWith("stale")) return "degraded";
  if (v.startsWith("starting") || v.startsWith("unbuilt")) return "pending";
  if (v === "healthy") return "ok";
  if (v === "degraded") return "degraded";
  if (v.startsWith("error")) return "error";
  // Hardening pass — the qdrant/llm forms added to /health.
  // "mock" is a real, deliberate posture, not a fault: it renders as a
  // distinct pending state so an operator can see at a glance that the
  // platform is answering from a stub rather than a provider.
  if (v.startsWith("mock")) return "pending";
  if (v.startsWith("unreachable") || v.startsWith("misconfigured")) return "error";
  if (v.startsWith("not configured")) return "disabled";
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
      // Phase E7: real heartbeat-backed worker supervision (OBS-3/4).
      monitor:    reachable ? normalize(checks.monitor_agent) : "unknown",
      ingestion:  reachable ? normalize(checks.ingestion_worker) : "unknown",
      bm25:       reachable ? normalize(checks.bm25_index) : "unknown",
      // Both are now really reported by /health (hardening pass). They were
      // hardcoded "unknown" placeholders because the backend did not expose
      // them — which meant an unreachable Qdrant (retrieval silently returns
      // nothing) and an expired LLM key (every investigation fails) both
      // rendered as a permanent grey "n/a" next to healthy-looking lights.
      qdrant:   reachable ? normalize(checks.qdrant) : "unknown",
      llm:      reachable ? normalize(checks.llm) : "unknown",
    },
    // Raw text (e.g. "stale (last heartbeat 130s ago)") for tooltips.
    checksRaw: checks,
    refresh: poll,
  };

  return <HealthContext.Provider value={value}>{children}</HealthContext.Provider>;
}

export function useHealth() {
  return useContext(HealthContext) || {
    reachable: false, deps: {}, overall: "unknown", refresh() {},
  };
}
