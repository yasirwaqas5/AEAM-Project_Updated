import { Badge, Icon, getCrossDatasetData } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Cross-Dataset Analysis view (Phase C4).
 *
 * Renders correlated signals the Cross-Dataset Intelligence engine found
 * across OTHER activated datasets during this investigation — a FOURTH,
 * structurally distinct evidence source from Knowledge Documents (RAG),
 * Enterprise Memory (past incidents), and Enterprise Policies (business
 * rules). See aeam/intelligence/cross_dataset_analyzer.py: this is
 * advisory-only correlation evidence, never a second MonitorAgent/
 * RuleEngine/ForecastAgent, and never capable of altering a deterministic
 * decision.
 *
 * Four honest states:
 * - Never consulted for this investigation (older incident, or the
 *   analyzer was unavailable at the time) — getCrossDatasetData() is null.
 * - Consulted but insufficient data (fewer than 2 activated datasets, or
 *   an unexpected failure) — data.insufficient_data === true, with the
 *   real reason shown, never glossed over.
 * - Consulted, found nothing (no supporting/contradicting/correlated
 *   signals across every other activated dataset checked).
 * - Consulted, found real evidence — rendered in four clearly-labelled
 *   groups (Supporting / Contradicting / Strong Correlations / Missing
 *   Signals), each entry carrying full traceability back to its dataset.
 * ────────────────────────────────────────────────────────────────────────── */

function DatasetEntryRow({ entry, kind }) {
  const color = kind === "supporting" ? "#00ffa3" : kind === "contradicting" ? "#ffb800" : "#00b4ff";
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem",
      padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(255,255,255,0.015)", flexWrap: "wrap",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", minWidth: 0 }}>
        <Icon name="database" size={12} color="var(--muted)" />
        <span style={{ fontSize: "0.78rem", color: "var(--text)", fontWeight: 600 }}>{entry.dataset_name || entry.dataset_id}</span>
        {entry.metric && <span style={{ fontSize: "0.7rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>· {entry.metric}</span>}
      </div>
      <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", alignItems: "center" }}>
        {entry.relation && <Badge label={entry.relation} color="var(--muted)" />}
        {entry.z_score != null && <Badge label={`z=${entry.z_score}`} color={color} />}
        {entry.correlation != null && <Badge label={`r=${entry.correlation}`} color={color} />}
        {entry.reason && <span style={{ fontSize: "0.68rem", color: "var(--muted)", fontStyle: "italic" }}>{entry.reason}</span>}
      </div>
    </div>
  );
}

function Section({ title, icon, color, entries, kind, emptyNote }) {
  if (!entries || entries.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
        <Icon name={icon} size={13} color={color} />
        <span style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)", fontWeight: 700 }}>
          {title} ({entries.length})
        </span>
      </div>
      {entries.map((e, i) => <DatasetEntryRow key={e.dataset_id ? `${e.dataset_id}-${i}` : i} entry={e} kind={kind} />)}
    </div>
  );
}

export default function CrossDatasetPanel({ incident }) {
  const data = getCrossDatasetData(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Cross-Dataset Intelligence was not consulted for this investigation.
      </div>
    );
  }

  if (data.insufficient_data) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        <Icon name="alert" size={16} style={{ marginBottom: "0.4rem", opacity: 0.7 }} /><br />
        Insufficient data for cross-dataset analysis.
        <div style={{ marginTop: "0.4rem", fontSize: "0.72rem" }}>{data.reason}</div>
      </div>
    );
  }

  const supporting = data.supporting || [];
  const contradicting = data.contradicting || [];
  const strongCorrelations = data.strong_correlations || [];
  const missingSignals = data.missing_signals || [];
  const nothingFound = supporting.length === 0 && contradicting.length === 0 && strongCorrelations.length === 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
        {data.origin_dataset_name
          ? <>Origin dataset: <strong style={{ color: "var(--text)" }}>{data.origin_dataset_name}</strong> — </>
          : "Origin dataset could not be resolved from the incident metric — "}
        checked against {data.candidates_checked ?? 0} other activated dataset(s)
      </div>

      {nothingFound && (
        <div style={{
          textAlign: "center", padding: "1.4rem 1rem", color: "var(--muted)",
          fontSize: "0.78rem", border: "1px dashed var(--border)", borderRadius: 10,
        }}>
          No supporting, contradicting, or strongly-correlated signals found.
        </div>
      )}

      <Section title="Supporting" icon="check" color="#00ffa3" entries={supporting} kind="supporting" />
      <Section title="Contradicting" icon="x" color="#ffb800" entries={contradicting} kind="contradicting" />
      <Section title="Strong Correlations" icon="branch" color="#00b4ff" entries={strongCorrelations} kind="correlation" />
      <Section title="Missing Signals" icon="alert" color="var(--muted)" entries={missingSignals} kind="missing" />
    </div>
  );
}
