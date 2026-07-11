import { StatusDot } from "../components/library";
import { useHealth } from "./HealthProvider";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/StatusBar.jsx
 * Always-visible footer: backend, database, redis, qdrant, llm + version.
 * Reads real signals from HealthProvider (/health + /system/status). Qdrant
 * and LLM show honest "n/a" until a backend probe exposes them (Phase A3).
 * ────────────────────────────────────────────────────────────────────────── */

const APP_VERSION = "v0.7.5"; // matches the backend tag line (git v0.7.4 + retrieval explainability)

function Dep({ label, state }) {
  const shown = state === "unknown" ? "n/a" : state;
  return (
    <span className="grp" title={`${label}: ${shown}`}>
      <StatusDot state={state} size={7} />
      <span className="k">{label}</span>
    </span>
  );
}

export default function StatusBar() {
  const { deps, updatedAt, reachable } = useHealth();
  return (
    <footer className="aeam-statusbar" aria-label="System status">
      <Dep label="Backend"  state={deps.backend} />
      <span className="sep">·</span>
      <Dep label="Postgres" state={deps.database} />
      <span className="sep">·</span>
      <Dep label="Redis"    state={deps.redis} />
      <span className="sep">·</span>
      <Dep label="Qdrant"   state={deps.qdrant} />
      <span className="sep">·</span>
      <Dep label="LLM"      state={deps.llm} />
      <span className="ver">
        {updatedAt ? `synced ${updatedAt.toLocaleTimeString()}` : (reachable ? "syncing…" : "offline")}
        {"  ·  AEAM "}{APP_VERSION}
      </span>
    </footer>
  );
}
