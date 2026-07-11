import { useState } from "react";
import { PageHeader, Button } from "../components/ui";
import { PageContainer, Panel } from "../components/library";

/* Business logic (form state + POST /api/v1/trigger/) is unchanged from the
 * original page — only the presentation is wrapped in the Enterprise shell. */

const FIELDS = [
  { name: "event_type", label: "Event Type", type: "text",   placeholder: "e.g. CPU_HIGH" },
  { name: "metric",     label: "Metric",     type: "text",   placeholder: "e.g. cpu_util" },
  { name: "value",      label: "Value",      type: "number", placeholder: "e.g. 97" },
  { name: "severity",   label: "Severity",   type: "text",   placeholder: "CRITICAL | HIGH | MEDIUM | LOW" },
];

const inputStyle = {
  width: "100%", background: "var(--bg)", border: "1px solid var(--border)",
  borderRadius: 8, color: "var(--text)", fontSize: "0.82rem",
  fontFamily: "var(--font-body)", padding: "0.55rem 0.7rem", outline: "none",
};

export default function Trigger() {
  const [form, setForm] = useState({ event_type: "", metric: "", value: "", severity: "" });
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState(null);

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handleSubmit = async () => {
    setSubmitted(false);
    setError(null);
    try {
      await fetch("/api/v1/trigger/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, value: parseFloat(form.value) }),
      });
      setSubmitted(true);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <PageContainer max={720}>
      <PageHeader title="Trigger Event" subtitle="Manually inject an anomaly event into the pipeline" />
      <Panel title="Event payload" icon="bolt">
        <div style={{ display: "grid", gap: "1.1rem" }}>
          {FIELDS.map((f) => (
            <label key={f.name} style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
              <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>{f.label}</span>
              <input name={f.name} type={f.type} value={form[f.name]} onChange={handleChange}
                placeholder={f.placeholder} style={inputStyle} />
            </label>
          ))}

          <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginTop: "0.3rem" }}>
            <Button variant="primary" icon="bolt" onClick={handleSubmit}>Trigger Event</Button>
            {submitted && <span style={{ color: "var(--ok)", fontSize: "0.8rem" }}>✓ Event triggered successfully.</span>}
            {error && <span style={{ color: "var(--err)", fontSize: "0.8rem", fontFamily: "var(--font-mono)" }}>✕ {error}</span>}
          </div>
        </div>
      </Panel>
    </PageContainer>
  );
}
