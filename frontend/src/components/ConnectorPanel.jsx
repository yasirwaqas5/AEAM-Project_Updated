import { Badge, Icon } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Connector panel (Phase F7).
 *
 * Renders GET /api/v1/connectors/health — the eight enterprise connectors,
 * their configuration state, and their synchronization outcomes. See
 * aeam/connectors/health.py: every field is either an observed fact or an
 * explicit absence with a reason.
 *
 * The honesty rules this component must not undo:
 *  - `stale: null` means "not computable" (the connector has never synced) and
 *    must NOT render as fresh. "We cannot tell" and "it is current" are
 *    different answers.
 *  - a never-synced connector is `unknown`, not healthy. Rendering it green
 *    would be the exact misrepresentation SEC-8 forbids.
 *  - no credential appears anywhere. The payload carries configuration KEYS
 *    and the secret's NAME; this component renders only those.
 *  - the catalog shows all eight connectors including unconfigured ones, so an
 *    operator can see what is available instead of reading the source.
 * ────────────────────────────────────────────────────────────────────────── */

const EMPTY_BOX = {
  textAlign: "center", padding: "1.6rem 1rem", color: "var(--muted)",
  fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
};

const SYNC_STATUS_COLOR = {
  succeeded: "var(--ok)",
  partial: "var(--warn)",
  failed: "var(--err)",
  running: "var(--info)",
  never_synced: "var(--muted)",
};

/* A connector's state, derived exactly as the backend summary derives it, so
   the badge and the summary counts can never disagree. */
function stateOf(connector) {
  if (!connector.enabled) return { label: "disabled", color: "var(--faint)" };
  if (!connector.configured) return { label: "not configured", color: "var(--warn)" };
  if (!connector.authenticated) return { label: "not authenticated", color: "var(--err)" };
  if (connector.sync_status === "failed") return { label: "sync failed", color: "var(--err)" };
  if (!connector.last_successful_sync) return { label: "never synced", color: "var(--muted)" };
  if (connector.stale) return { label: "stale", color: "var(--warn)" };
  return { label: "healthy", color: "var(--ok)" };
}

function secondsLabel(value) {
  if (value == null) return null;
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  return `${Math.round(value / 3600)}h`;
}

function CountRow({ connector }) {
  const counts = [
    ["listed", connector.listed_count],
    ["changed", connector.changed_count],
    ["processed", connector.processed_count],
    ["skipped", connector.skipped_count],
    ["failed", connector.failed_count],
  ];
  return (
    <div style={{ display: "flex", gap: "0.35rem", flexWrap: "wrap" }}>
      {counts.map(([label, value]) => (
        <span key={label} style={{
          fontSize: "0.64rem", fontFamily: "var(--font-mono)", color: "var(--muted)",
          border: "1px solid var(--border)", borderRadius: 6, padding: "0.1rem 0.4rem",
        }}>{label}={value ?? 0}</span>
      ))}
      {connector.known_artifacts != null && (
        <span style={{
          fontSize: "0.64rem", fontFamily: "var(--font-mono)", color: "var(--muted)",
          border: "1px solid var(--border)", borderRadius: 6, padding: "0.1rem 0.4rem",
        }}>known={connector.known_artifacts}</span>
      )}
    </div>
  );
}

function ConnectorRow({ connector, onSync, syncing }) {
  const state = stateOf(connector);
  const statusColor = SYNC_STATUS_COLOR[connector.sync_status] || "var(--muted)";
  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: 8, padding: "0.6rem 0.8rem",
      background: "rgba(255,255,255,0.015)", display: "flex", flexDirection: "column", gap: "0.4rem",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.6rem", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", minWidth: 0 }}>
          <Icon name="database" size={12} color="var(--muted)" />
          <span style={{ fontSize: "0.82rem", fontWeight: 600, color: "var(--text)" }}>
            {connector.display_name || connector.kind}
          </span>
          <span style={{ fontSize: "0.66rem", color: "var(--faint)", fontFamily: "var(--font-mono)" }}>
            {connector.name || connector.source_id}
          </span>
        </div>
        <div style={{ display: "flex", gap: "0.35rem", flexWrap: "wrap", alignItems: "center" }}>
          <Badge label={connector.capability} color="var(--muted)" />
          <Badge label={state.label} color={state.color} dot />
          <Badge label={connector.sync_status} color={statusColor} />
          {onSync && (
            <button
              onClick={() => onSync(connector.source_id)}
              disabled={syncing || !connector.enabled}
              title={connector.enabled ? "Run a synchronization now" : `Enable ${connector.flag} first`}
              style={{
                fontSize: "0.66rem", padding: "0.15rem 0.5rem", borderRadius: 6,
                border: "1px solid var(--border)", background: "transparent",
                color: connector.enabled ? "var(--text)" : "var(--faint)",
                cursor: connector.enabled && !syncing ? "pointer" : "not-allowed",
              }}
            >{syncing ? "Syncing…" : "Sync"}</button>
          )}
        </div>
      </div>

      <CountRow connector={connector} />

      <div style={{ fontSize: "0.68rem", color: "var(--muted)", display: "flex", gap: "0.9rem", flexWrap: "wrap" }}>
        <span>
          last success:{" "}
          {connector.last_successful_sync
            ? `${connector.last_successful_sync}${
                connector.last_successful_sync_age_seconds != null
                  ? ` (${secondsLabel(connector.last_successful_sync_age_seconds)} ago)` : ""
              }`
            : "never"}
        </span>
        {connector.last_failed_sync && <span>last failure: {connector.last_failed_sync}</span>}
        {connector.sync_duration_seconds != null && (
          <span>duration: {secondsLabel(connector.sync_duration_seconds)}</span>
        )}
        {/* stale === null must never read as fresh. */}
        <span>
          stale: {connector.stale == null ? "not computable" : connector.stale ? "yes" : "no"}
        </span>
      </div>

      {connector.stale == null && connector.stale_reason && (
        <span style={{ fontSize: "0.64rem", color: "var(--faint)", fontStyle: "italic" }}>
          {connector.stale_reason}
        </span>
      )}
      {connector.configuration_reason && (
        <span style={{ fontSize: "0.66rem", color: "var(--warn)" }}>
          <Icon name="alert" size={10} color="var(--warn)" /> {connector.configuration_reason}
        </span>
      )}
      {connector.error_reason && (
        <span style={{ fontSize: "0.66rem", color: "var(--err)" }}>
          <Icon name="alert" size={10} color="var(--err)" /> {connector.error_reason}
        </span>
      )}
      {/* Configuration KEYS and the secret NAME only — never a value. */}
      <span style={{ fontSize: "0.62rem", color: "var(--faint)", fontFamily: "var(--font-mono)" }}>
        config: [{(connector.config_keys || []).join(", ") || "none"}] · secret_ref:{" "}
        {connector.secret_ref || "unset"}
      </span>
    </div>
  );
}

function CatalogRow({ entry }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.6rem",
      padding: "0.35rem 0.7rem", border: "1px solid var(--border)", borderRadius: 8, flexWrap: "wrap",
    }}>
      <span style={{ fontSize: "0.76rem", color: "var(--text)" }}>
        {entry.display_name}
        <span style={{ fontSize: "0.64rem", color: "var(--faint)" }}> · {entry.capability}</span>
      </span>
      <div style={{ display: "flex", gap: "0.35rem", alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.62rem", fontFamily: "var(--font-mono)", color: "var(--faint)" }}>
          {(entry.required_config || []).join(", ") || "no required config"}
        </span>
        <Badge label={entry.enabled ? "enabled" : "disabled"}
          color={entry.enabled ? "var(--ok)" : "var(--faint)"} dot />
      </div>
    </div>
  );
}

export default function ConnectorPanel({ health, onSync, syncingId }) {
  if (!health) {
    return <div style={EMPTY_BOX}>Connector state has not been queried.</div>;
  }

  const connectors = health.connectors || [];
  const catalog = health.catalog || [];
  const summary = health.summary || {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.8rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.74rem", color: "var(--muted)" }}>
          {health.framework_enabled
            ? `${summary.enabled ?? 0} of ${summary.total ?? 0} registered connector(s) enabled`
            : "No connectors are enabled — the platform is on its upload + Google Sheets posture."}
        </span>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          <Badge label={`${summary.healthy ?? 0} healthy`} color="var(--ok)" dot />
          <Badge label={`${summary.unhealthy ?? 0} unhealthy`} color="var(--err)" dot />
          {/* `unknown` is its own bucket: a never-synced connector is neither. */}
          <Badge label={`${summary.unknown ?? 0} unknown`} color="var(--muted)" dot />
          {health.mock_mode && <Badge label="MOCK MODE" color="var(--warn)" dot />}
        </div>
      </div>

      {health.mock_mode && (
        <div style={{
          fontSize: "0.7rem", color: "var(--warn)", border: "1px solid var(--border)",
          borderRadius: 8, padding: "0.5rem 0.7rem",
        }}>
          <Icon name="alert" size={12} color="var(--warn)" /> Connectors are running against
          deterministic in-repo mock clients, not real upstream systems. Ingested content is
          fixture data.
        </div>
      )}

      {health.reason && (
        <div style={{ fontSize: "0.7rem", color: "var(--err)" }}>{health.reason}</div>
      )}

      <div>
        <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
          Registered connectors ({connectors.length})
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", marginTop: "0.4rem" }}>
          {connectors.length === 0 ? (
            <div style={EMPTY_BOX}>
              No connector sources are registered. Register one as a `sources` row of a
              connector kind, then enable its flag.
            </div>
          ) : (
            connectors.map((c) => (
              <ConnectorRow key={c.source_id} connector={c} onSync={onSync}
                syncing={syncingId === c.source_id} />
            ))
          )}
        </div>
      </div>

      <div>
        <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
          Available connector types ({catalog.length})
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem", marginTop: "0.4rem" }}>
          {catalog.map((entry) => <CatalogRow key={entry.kind} entry={entry} />)}
        </div>
      </div>

      <span style={{ fontSize: "0.62rem", color: "var(--faint)", fontStyle: "italic", lineHeight: 1.5 }}>
        Connector content enters through the same ingestion pipeline as an uploaded file, so a
        synced document is retrievable identically to one you upload. Credentials are resolved
        through SecretManager at sync time and never stored, logged, or shown here.
      </span>
    </div>
  );
}
