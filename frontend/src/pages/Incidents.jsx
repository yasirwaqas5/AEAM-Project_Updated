import { useState, useEffect, useCallback } from "react";
import {
  UIStyles, PageHeader, Card, Button, Modal, Badge, Field, ConfidenceBar,
  SeverityBadge, Skeleton, Icon,
  severityOf, deriveStatus, getRetrievedCount, getRecommendedAction,
  fmtTime, fmtRelative,
} from "../components/ui";
import { fetchPage } from "../lib/api";
import EvidencePanel from "../components/EvidencePanel";
import Timeline from "../components/Timeline";

// ─── Data fetching (Phase E6: bounded paged consumption) ──────────────────────
//
// The incidents list can grow without bound. Rather than pulling the whole
// table into the browser on every load, fetch a bounded page and let the
// operator "Load more" on demand. The backend endpoint stays
// backward-compatible (a parameter-less call still returns everything); this
// page opts into the E6 limit/offset + X-Total-Count contract.

const PAGE_SIZE = 100;

const SEVERITY_FILTERS = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];

// ─── Incident card ────────────────────────────────────────────────────────────

function IncidentCard({ incident, onOpen }) {
  const sev = severityOf(incident.severity);
  const status = deriveStatus(incident);
  const evidenceCount = getRetrievedCount(incident);

  return (
    <Card className="aeam-card-hover" style={{ borderLeft: `3px solid ${sev.color}`, padding: "1.25rem 1.4rem" }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.7rem", minWidth: 0 }}>
          <SeverityBadge severity={incident.severity} />
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.82rem", fontWeight: 600, color: "var(--text)" }}>
            {incident.event_type ?? "—"}
          </span>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>· {incident.metric ?? "—"}</span>
        </div>
        <Badge label={status.label} color={status.color} dot />
      </div>

      {/* Structured sections */}
      <div className="aeam-grid-auto" style={{ margin: "1.1rem 0", gap: "1rem" }}>
        <Field label="Root Cause" value={incident.root_cause || "Pending"} />
        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
          <ConfidenceBar value={incident.confidence} />
        </div>
        <Field label="Recommended Action" value={getRecommendedAction(incident)} />
        <Field label="Timestamp" value={fmtTime(incident.timestamp)} title={incident.timestamp} />
      </div>

      {/* Footer: meta + actions */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap", paddingTop: "0.9rem", borderTop: "1px solid var(--border)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "1.1rem", fontSize: "0.7rem", color: "var(--muted)" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}><Icon name="database" size={12} /> {evidenceCount} evidence</span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}><Icon name="layers" size={12} /> depth {incident.investigation_depth ?? "—"}</span>
          <span className="aeam-hide-sm" style={{ fontFamily: "var(--font-mono)" }}>{fmtRelative(incident.timestamp)}</span>
        </div>
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          <Button icon="database" onClick={() => onOpen("evidence", incident)}>View Evidence</Button>
          <Button icon="branch" onClick={() => onOpen("timeline", incident)}>View Timeline</Button>
          <Button icon="code" onClick={() => onOpen("json", incident)}>View Raw JSON</Button>
        </div>
      </div>
    </Card>
  );
}

// ─── Card skeleton ────────────────────────────────────────────────────────────

function CardSkeleton() {
  return (
    <Card style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <Skeleton width={220} height={16} />
        <Skeleton width={90} height={22} style={{ borderRadius: 20 }} />
      </div>
      <div className="aeam-grid-auto">
        {[1, 2, 3, 4].map((i) => <Skeleton key={i} height={34} />)}
      </div>
    </Card>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────────

export default function Incidents() {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("ALL");
  const [modal, setModal] = useState(null); // { kind, incident }
  const [total, setTotal] = useState(null);
  const [hasMore, setHasMore] = useState(false);

  // Server-side severity filter maps to the E6 `severity` query param so we
  // never over-fetch; "ALL" fetches unfiltered. Changing the filter reloads
  // page one.
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const params = filter !== "ALL" ? { severity: filter } : {};
      const page = await fetchPage("/api/v1/incidents/", { limit: PAGE_SIZE, offset: 0, params });
      setIncidents(page.items);
      setTotal(page.total);
      setHasMore(page.hasMore);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  const loadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const params = filter !== "ALL" ? { severity: filter } : {};
      const page = await fetchPage("/api/v1/incidents/", {
        limit: PAGE_SIZE, offset: incidents.length, params,
      });
      setIncidents((prev) => [...prev, ...page.items]);
      setTotal(page.total);
      setHasMore(page.hasMore);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoadingMore(false);
    }
  }, [filter, incidents.length]);

  useEffect(() => { load(); }, [load]);

  // Severity filtering now happens server-side; sort the loaded page by rank
  // for display consistency with the pre-E6 behaviour.
  const sorted = [...incidents].sort((a, b) => severityOf(b.severity).rank - severityOf(a.severity).rank);

  const openModal = (kind, incident) => setModal({ kind, incident });
  const closeModal = () => setModal(null);

  const MODAL_META = {
    evidence: { title: "Retrieved Evidence", icon: "database" },
    timeline: { title: "Investigation Timeline", icon: "branch" },
    json:     { title: "Raw Incident JSON", icon: "code" },
  };

  return (
    <>
      <UIStyles />
      <div className="aeam-page">
        <PageHeader
          title="Incidents"
          subtitle="All processed anomaly incidents"
          right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
        />

        {/* Severity filters */}
        <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1.5rem", flexWrap: "wrap" }}>
          {SEVERITY_FILTERS.map((f) => {
            const active = filter === f;
            return (
              <button key={f} onClick={() => setFilter(f)} style={{
                fontSize: "0.7rem", letterSpacing: "0.08em", textTransform: "uppercase",
                background: active ? "var(--accent-dim)" : "none",
                border: `1px solid ${active ? "var(--accent-border)" : "var(--border)"}`,
                color: active ? "var(--accent)" : "var(--muted)",
                borderRadius: 6, padding: "0.3rem 0.75rem", cursor: "pointer", transition: "all 0.15s",
              }}>{f}</button>
            );
          })}
        </div>

        {!loading && !error && (
          <div style={{ fontSize: "0.72rem", color: "var(--muted)", letterSpacing: "0.06em", marginBottom: "1.1rem" }}>
            Showing {sorted.length}
            {total != null ? ` of ${total}` : ""} incident{total !== 1 ? "s" : ""}
            {filter !== "ALL" && ` · filtered by ${filter}`}
          </div>
        )}

        {error && (
          <div style={{ background: "rgba(255,95,87,0.08)", border: "1px solid rgba(255,95,87,0.25)", borderRadius: 10, padding: "1rem 1.25rem", color: "var(--err)", fontSize: "0.8rem", fontFamily: "var(--font-mono)" }}>
            ⚠ Failed to load incidents: {error}
          </div>
        )}

        {loading && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
            {[1, 2, 3].map((i) => <CardSkeleton key={i} />)}
          </div>
        )}

        {!loading && !error && sorted.length === 0 && (
          <div style={{ border: "1px dashed var(--border)", borderRadius: 12, padding: "3rem", textAlign: "center", color: "var(--muted)", fontSize: "0.82rem" }}>
            {filter === "ALL" ? "No incidents recorded yet." : `No ${filter} incidents found.`}
          </div>
        )}

        {!loading && !error && sorted.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
            {sorted.map((inc) => (
              <IncidentCard key={inc.incident_id ?? Math.random()} incident={inc} onOpen={openModal} />
            ))}
          </div>
        )}

        {/* Phase E6: bounded pagination — fetch the next page on demand
            rather than pulling the entire table into the browser at once. */}
        {!loading && !error && hasMore && (
          <div style={{ display: "flex", justifyContent: "center", marginTop: "1.4rem" }}>
            <Button icon="layers" onClick={loadMore} disabled={loadingMore}>
              {loadingMore ? "Loading…" : `Load more${total != null ? ` (${total - sorted.length} remaining)` : ""}`}
            </Button>
          </div>
        )}
      </div>

      {/* Modals */}
      {modal && (
        <Modal
          title={MODAL_META[modal.kind].title}
          icon={MODAL_META[modal.kind].icon}
          onClose={closeModal}
        >
          <div style={{ marginBottom: "1rem", display: "flex", alignItems: "center", gap: "0.7rem", flexWrap: "wrap" }}>
            <SeverityBadge severity={modal.incident.severity} />
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.78rem", color: "var(--muted)" }}>
              {modal.incident.incident_id}
            </span>
          </div>
          {modal.kind === "evidence" && <EvidencePanel incident={modal.incident} />}
          {modal.kind === "timeline" && <Timeline incident={modal.incident} />}
          {modal.kind === "json" && (
            <pre className="aeam-json">{JSON.stringify(modal.incident, null, 2)}</pre>
          )}
        </Modal>
      )}
    </>
  );
}
