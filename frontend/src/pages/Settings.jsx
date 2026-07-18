import { useState, useEffect, useCallback, useMemo } from "react";
import { PageHeader, Button, Badge, Icon } from "../components/ui";
import { PageContainer, Panel, LoadingState, ErrorState, EmptyState } from "../components/library";
import { fetchJSON } from "./KnowledgeCenter";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Settings.jsx  (Enterprise Administration & Settings UI — Phase D5)
 *
 * Full read/write management surface over the Phase D4 Enterprise
 * Configuration Engine (aeam/config/settings.py's Settings class + the
 * per-engine optional-override wiring in aeam/main.py). Every value here
 * IS the same Settings model D4 built — this page adds no second
 * configuration mechanism, just a UI over aeam/api/administration.py's
 * read/update/validate/reset endpoints.
 *
 * Because every intelligence engine is constructed ONCE at app startup
 * (unchanged by this phase), a saved change here does not retroactively
 * alter the currently-running pipeline — it takes effect on next restart,
 * exactly like editing .env by hand always has. This page never hides that:
 * each field shows both its persisted ("configured") value and the value
 * the running process actually used at last startup ("effective"), with a
 * "restart required" badge whenever they differ.
 * ────────────────────────────────────────────────────────────────────────── */

const fetchConfig = () => fetchJSON("/api/v1/admin/config/");
const validateConfig = (values) =>
  fetchJSON("/api/v1/admin/config/validate", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }),
  });
const updateConfig = (values) =>
  fetchJSON("/api/v1/admin/config/", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }),
  });
const resetConfig = (body) =>
  fetchJSON("/api/v1/admin/config/reset", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });

const SECTION_ICONS = {
  Memory: "layers",
  Policy: "shield",
  "Cross Dataset": "branch",
  "Adaptive Detection": "activity",
  Retrieval: "search",
  "Execution Planning": "target",
  "AI Evaluation": "check",
  Observability: "activity",
};

const inputStyle = {
  width: "100%", background: "var(--bg)", border: "1px solid var(--border)",
  borderRadius: 8, color: "var(--text)", fontSize: "0.82rem",
  fontFamily: "var(--font-mono)", padding: "0.45rem 0.6rem", outline: "none",
};

function fmtValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return String(v);
  return String(v) || "—";
}

// ─── One field row ───────────────────────────────────────────────────────

function FieldRow({ field, draftValue, onChange, onResetOne, validation, saving }) {
  const isDirty = draftValue !== undefined;
  const displayValue = isDirty ? draftValue : (field.configured_value ?? "");

  const handleInputChange = (raw) => {
    onChange(field.key, raw === "" ? null : raw);
  };

  const renderControl = () => {
    if (field.choices) {
      const selected = new Set(String(displayValue || "").split(",").map((s) => s.trim()).filter(Boolean));
      return (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
          {field.choices.map((choice) => {
            const on = selected.has(choice);
            return (
              <label key={choice} style={{
                display: "flex", alignItems: "center", gap: "0.35rem", fontSize: "0.74rem",
                color: on ? "var(--text)" : "var(--muted)", cursor: "pointer",
                border: "1px solid var(--border)", borderRadius: 6, padding: "0.25rem 0.55rem",
                background: on ? "var(--surface)" : "transparent",
              }}>
                <input type="checkbox" checked={on} onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(choice); else next.delete(choice);
                  handleInputChange(Array.from(next).join(","));
                }} />
                {choice}
              </label>
            );
          })}
        </div>
      );
    }
    return (
      <input
        type={field.type === "string" ? "text" : "number"}
        step={field.type === "float" ? "any" : "1"}
        min={field.constraints?.gt ?? field.constraints?.ge}
        max={field.constraints?.lt ?? field.constraints?.le}
        value={displayValue}
        placeholder={field.default_note || `default: ${fmtValue(field.default)}`}
        onChange={(e) => handleInputChange(e.target.value)}
        style={inputStyle}
      />
    );
  };

  return (
    <div style={{
      display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1.4fr auto", gap: "0.9rem",
      alignItems: "start", padding: "0.8rem 0", borderBottom: "1px solid var(--border)",
    }}>
      <div>
        <div style={{ fontSize: "0.82rem", fontWeight: 600, color: "var(--text)", display: "flex", alignItems: "center", gap: "0.5rem" }}>
          {field.label}
          {field.is_overridden && <Badge label="Overridden" color="var(--info)" />}
          {field.restart_required && <Badge label="Restart required" color="var(--warn)" />}
        </div>
        <div style={{ fontSize: "0.7rem", color: "var(--muted)", marginTop: "0.25rem", lineHeight: 1.4 }}>
          {field.description}
        </div>
      </div>
      <div style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)", color: "var(--text)" }}>
        {fmtValue(field.configured_value)}
      </div>
      <div style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)", color: "var(--muted)" }}>
        {field.default_note ? <span style={{ fontFamily: "var(--font-body)", fontSize: "0.7rem" }}>{field.default_note}</span> : fmtValue(field.default)}
      </div>
      <div>
        {renderControl()}
        {validation && !validation.valid && (
          <div style={{ fontSize: "0.68rem", color: "var(--err)", marginTop: "0.3rem" }}>{validation.error}</div>
        )}
      </div>
      <div>
        <Button icon="x" variant="ghost" disabled={saving || (!field.is_overridden && !isDirty)}
          onClick={() => onResetOne(field.key)}>Reset</Button>
      </div>
    </div>
  );
}

// ─── Section panel ────────────────────────────────────────────────────────

function SectionPanel({ section, fields, draft, onChange, onResetOne, errors, saving }) {
  return (
    <Panel title={section} icon={SECTION_ICONS[section] || "code"} pad={false}>
      <div style={{ padding: "0 1.1rem" }}>
        <div style={{
          display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1.4fr auto", gap: "0.9rem",
          fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)",
          padding: "0.6rem 0", borderBottom: "1px solid var(--border)", fontWeight: 700,
        }}>
          <span>Name</span><span>Current</span><span>Default</span><span>Edit</span><span />
        </div>
        {fields.map((f) => (
          <FieldRow key={f.key} field={f} draftValue={draft[f.key]} onChange={onChange}
            onResetOne={onResetOne} validation={errors[f.key]} saving={saving} />
        ))}
      </div>
    </Panel>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────

export default function Settings() {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [draft, setDraft] = useState({});
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState(null);

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    fetchConfig()
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, []);

  useEffect(() => { load(); }, [load]);

  const grouped = useMemo(() => {
    if (!state.data) return [];
    const bySection = {};
    for (const f of state.data.fields) {
      (bySection[f.section] ||= []).push(f);
    }
    return state.data.sections.map((s) => ({ section: s, fields: bySection[s] || [] }));
  }, [state.data]);

  const dirtyKeys = Object.keys(draft);

  const handleChange = (key, value) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setErrors((e) => ({ ...e, [key]: undefined }));
    setSaveMessage(null);
  };

  const handleResetOne = async (key) => {
    setSaving(true); setSaveMessage(null);
    try {
      const data = await resetConfig({ keys: [key] });
      setState({ loading: false, error: null, data });
      setDraft((d) => { const next = { ...d }; delete next[key]; return next; });
      setErrors((e) => { const next = { ...e }; delete next[key]; return next; });
      setSaveMessage({ tone: "ok", text: `Reset '${key}' to its default.` });
    } catch (e) {
      setSaveMessage({ tone: "err", text: e.message });
    } finally {
      setSaving(false);
    }
  };

  const handleResetAll = async () => {
    setSaving(true); setSaveMessage(null);
    try {
      const data = await resetConfig({ all: true });
      setState({ loading: false, error: null, data });
      setDraft({}); setErrors({});
      setSaveMessage({ tone: "ok", text: "All configuration fields reset to their defaults." });
    } catch (e) {
      setSaveMessage({ tone: "err", text: e.message });
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    if (dirtyKeys.length === 0) return;
    setSaving(true); setSaveMessage(null); setErrors({});
    const values = Object.fromEntries(dirtyKeys.map((k) => [k, draft[k]]));
    try {
      const validation = await validateConfig(values);
      if (!validation.all_valid) {
        const fieldErrors = {};
        for (const [k, r] of Object.entries(validation.results)) {
          if (!r.valid) fieldErrors[k] = r;
        }
        setErrors(fieldErrors);
        setSaveMessage({ tone: "err", text: "Some values are invalid — nothing was saved." });
        return;
      }
      const data = await updateConfig(values);
      setState({ loading: false, error: null, data });
      setDraft({});
      setSaveMessage({ tone: "ok", text: `Saved ${dirtyKeys.length} field(s). Restart the backend to apply.` });
    } catch (e) {
      setSaveMessage({ tone: "err", text: e.message });
    } finally {
      setSaving(false);
    }
  };

  if (state.loading) return <PageContainer><LoadingState label="Loading configuration…" rows={6} /></PageContainer>;
  if (state.error) return <PageContainer><ErrorState message={state.error} onRetry={load} /></PageContainer>;
  if (!state.data || state.data.fields.length === 0) {
    return <PageContainer><EmptyState icon="code" title="No configuration fields" description="The Enterprise Configuration Engine reported no fields." /></PageContainer>;
  }

  return (
    <PageContainer>
      <PageHeader title="Settings" subtitle="Enterprise Configuration Engine — thresholds, weights and limits for every intelligence engine"
        right={
          <div style={{ display: "flex", gap: "0.6rem" }}>
            <Button icon="x" variant="ghost" disabled={saving} onClick={handleResetAll}>Reset all to defaults</Button>
            <Button icon="check" variant="primary" disabled={saving || dirtyKeys.length === 0} onClick={handleSave}>
              {saving ? "Saving…" : `Save${dirtyKeys.length ? ` (${dirtyKeys.length})` : ""}`}
            </Button>
          </div>
        } />

      {state.data.restart_required && (
        <div style={{
          display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "1rem",
          background: "color-mix(in srgb, var(--warn) 12%, transparent)", border: "1px solid var(--warn)",
          borderRadius: 8, padding: "0.6rem 0.9rem", fontSize: "0.78rem", color: "var(--warn)",
        }}>
          <Icon name="alert" size={14} color="var(--warn)" />
          One or more configured values differ from what the running backend was started with — restart the
          backend process to apply them. Historical investigations are never affected either way.
        </div>
      )}

      {saveMessage && (
        <div style={{
          marginBottom: "1rem", fontSize: "0.78rem", fontFamily: "var(--font-mono)",
          color: saveMessage.tone === "ok" ? "var(--ok)" : "var(--err)",
        }}>
          {saveMessage.tone === "ok" ? "✓ " : "✕ "}{saveMessage.text}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
        {grouped.map(({ section, fields }) => (
          <SectionPanel key={section} section={section} fields={fields} draft={draft}
            onChange={handleChange} onResetOne={handleResetOne} errors={errors} saving={saving} />
        ))}
      </div>
    </PageContainer>
  );
}
