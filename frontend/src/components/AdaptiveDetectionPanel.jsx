import { Badge, Icon, getAdaptiveDetectionData } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Adaptive Detection view (Phase C5).
 *
 * Renders the longer-horizon rolling baseline and day-of-week seasonality
 * judgement the Adaptive Detection Engine computed for this incident's own
 * metric, combined with the event's already-computed statistical/forecast
 * signals — a FIFTH, structurally distinct evidence source from Knowledge
 * Documents (RAG), Enterprise Memory (past incidents), Enterprise Policies
 * (business rules), and Cross-Dataset Intelligence (other datasets). See
 * aeam/intelligence/adaptive_detection.py: this reuses StatisticalDetector
 * unmodified (a second, longer-window instance) and only READS the event's
 * already-computed statistical/forecast metadata — never a second
 * MonitorAgent, RuleEngine, or ForecastAgent invocation, and never capable
 * of altering a deterministic decision.
 *
 * Honest states, independently for baseline and seasonality:
 * - Never consulted for this investigation (older incident, or the engine
 *   was unavailable at the time) — getAdaptiveDetectionData() is null.
 * - Consulted but insufficient history for that sub-analysis —
 *   adaptive_baseline_insufficient / seasonality_insufficient carries the
 *   real reason, never glossed over.
 * - Consulted, real result — rendered with its own values, plus a combined-
 *   signal summary of every corroborating source.
 * ────────────────────────────────────────────────────────────────────────── */

function InsufficientNote({ children }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: "0.5rem",
      padding: "0.55rem 0.8rem", border: "1px dashed var(--border)", borderRadius: 8,
      color: "var(--muted)", fontSize: "0.72rem",
    }}>
      <Icon name="alert" size={12} color="var(--muted)" style={{ marginTop: "0.1rem", flexShrink: 0 }} />
      <span>{children}</span>
    </div>
  );
}

function BaselineSection({ data }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
        <Icon name="branch" size={13} color="var(--info)" />
        <span style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)", fontWeight: 700 }}>
          Adaptive Baseline
        </span>
      </div>
      {data.adaptive_baseline_insufficient ? (
        <InsufficientNote>{data.adaptive_baseline_insufficient}</InsufficientNote>
      ) : (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem",
          padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
          background: "rgba(255,255,255,0.015)", flexWrap: "wrap",
        }}>
          <span style={{ fontSize: "0.78rem", color: "var(--text)" }}>
            moving_avg=<strong>{data.adaptive_baseline?.moving_avg}</strong>{"  "}
            z_score=<strong>{data.adaptive_baseline?.z_score}</strong>
          </span>
          <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <Badge label={`history=${data.history_points_used ?? 0}`} color="var(--muted)" />
            {data.adaptive_baseline?.statistical_anomaly
              ? <Badge label="anomaly" color="var(--warn)" />
              : <Badge label="normal" color="var(--ok)" />}
          </div>
        </div>
      )}
    </div>
  );
}

function SeasonalitySection({ data }) {
  const seasonality = data.seasonality;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
        <Icon name="database" size={13} color="var(--info)" />
        <span style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)", fontWeight: 700 }}>
          Seasonality
        </span>
      </div>
      {data.seasonality_insufficient ? (
        <InsufficientNote>{data.seasonality_insufficient}</InsufficientNote>
      ) : seasonality?.detected ? (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem",
          padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
          background: "rgba(255,255,255,0.015)", flexWrap: "wrap",
        }}>
          <span style={{ fontSize: "0.78rem", color: "var(--text)" }}>
            Highest: <strong>{seasonality.highest_weekday}</strong> · Lowest: <strong>{seasonality.lowest_weekday}</strong>
          </span>
          <Badge label={`strength=${seasonality.strength}`} color="var(--info)" />
        </div>
      ) : (
        <div style={{
          textAlign: "center", padding: "0.9rem 0.8rem", color: "var(--muted)",
          fontSize: "0.75rem", border: "1px dashed var(--border)", borderRadius: 8,
        }}>
          No significant weekday seasonality detected.
          {seasonality?.reason && <div style={{ marginTop: "0.3rem", fontSize: "0.7rem" }}>{seasonality.reason}</div>}
        </div>
      )}
    </div>
  );
}

export default function AdaptiveDetectionPanel({ incident }) {
  const data = getAdaptiveDetectionData(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Adaptive Detection Engine was not consulted for this investigation.
      </div>
    );
  }

  const corroborating = data.corroborating_signals || [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <BaselineSection data={data} />
      <SeasonalitySection data={data} />

      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem",
        padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
        background: "rgba(255,255,255,0.015)", flexWrap: "wrap",
      }}>
        <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
          {data.combined_signal
            ? <>Corroborated by: <strong style={{ color: "var(--text)" }}>{corroborating.join(", ")}</strong></>
            : "No corroborating evidence across adaptive baseline, statistical, or forecast signals."}
        </span>
        {data.combined_signal
          ? <Badge label="combined signal" color="var(--warn)" />
          : <Badge label="no signal" color="var(--muted)" />}
      </div>
    </div>
  );
}
