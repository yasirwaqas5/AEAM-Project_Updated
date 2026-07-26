import { StatusDot } from "../components/library";
import { useHealth } from "./HealthProvider";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/StatusBar.jsx
 * Always-visible footer: backend, database, redis, monitor, ingestion,
 * qdrant, llm + version. Reads real signals from HealthProvider
 * (/health + /system/status). Qdrant and LLM show honest "n/a" until a
 * backend probe exposes them (Phase A3). Monitor/Ingestion are real
 * heartbeat-backed supervision chips (Phase E7, OBS-3/4) — "stale" means
 * the worker's thread has stopped updating its heartbeat.
 * ────────────────────────────────────────────────────────────────────────── */


function Dep({ label, state, raw }) {
  const shown = state === "unknown" ? "n/a" : (raw || state);
  return (
    <span className="grp" title={`${label}: ${shown}`}>
      <StatusDot state={state} size={7} />
      <span className="k">{label}</span>
    </span>
  );
}

export default function StatusBar() {
  const { deps, checksRaw, updatedAt, reachable } = useHealth();
  const raw = checksRaw || {};
  return (
    <footer className="aeam-statusbar" aria-label="System status">
      <Dep label="Backend"   state={deps.backend} />
      <span className="sep">·</span>
      <Dep label="Postgres"  state={deps.database} />
      <span className="sep">·</span>
      <Dep label="Redis"     state={deps.redis} />
      <span className="sep">·</span>
      <Dep label="Monitor"   state={deps.monitor} raw={raw.monitor_agent} />
      <span className="sep">·</span>
      <Dep label="Ingestion" state={deps.ingestion} raw={raw.ingestion_worker} />
      <span className="sep">·</span>
      <Dep label="Qdrant"    state={deps.qdrant} />
      <span className="sep">·</span>
      <Dep label="LLM"       state={deps.llm} />
      <span className="ver">
        {updatedAt ? `synced ${updatedAt.toLocaleTimeString()}` : (reachable ? "syncing…" : "offline")}
        {"  ·  AEAM Console"}
      </span>
    </footer>
  );
}
