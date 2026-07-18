import { useState, useEffect, useCallback, useMemo } from "react";
import { PageHeader, Badge, Modal, Field, Skeleton, Button, Icon, fmtTime, fmtRelative } from "../components/ui";
import { PageContainer, MetricCard, Panel, DataTable, LoadingState, ErrorState, EmptyState } from "../components/library";
import {
  fetchJSON, fetchDatasets, fetchDatasetDetail, fetchDatasetPreview, fetchVersions,
  reindexDataset, deleteDataset, pollJob, statusColor, StatusBadge, SearchBox,
  ConfirmDeleteModal, PreviewPanel, VersionHistoryTable, UploadDropzone,
} from "./KnowledgeCenter";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/DataCenter.jsx  (Enterprise Data Center)
 *
 * Exposes the structured-data capabilities already built (B1.1–B1.7): the
 * dataset catalog, schema, and actions all reuse aeam/api/knowledge.py's
 * EXISTING dataset endpoints verbatim (imported from KnowledgeCenter.jsx —
 * no duplicated fetch/UI logic). The only genuinely new surface is
 * activation (activate/deactivate — aeam/api/data_center.py, backed by the
 * new RedisDatasetActivation) and the composed business/monitoring profile
 * (same file, pure composition of the existing DatasetIntelligenceService +
 * activation + RuleEngine — no new business logic). Upload also mounts here
 * (KnowledgeCenter.jsx's UploadDropzone, reused verbatim — same component,
 * same /api/v1/ingest/upload call, same job polling, not duplicated) so a
 * CSV/Excel dataset can be registered without a trip to Knowledge Center.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Data Center-specific fetchers (new endpoints only) ─────────────────────

const fetchActivation = () => fetchJSON("/api/v1/data-center/activation");
const fetchDatasetProfile = (id) => fetchJSON(`/api/v1/data-center/datasets/${id}/profile`);
const activateDataset = (id) => fetchJSON(`/api/v1/data-center/datasets/${id}/activate`, { method: "POST" });
const deactivateDataset = (id) => fetchJSON(`/api/v1/data-center/datasets/${id}/deactivate`, { method: "POST" });

// ─── Business Profile & Monitoring panel ────────────────────────────────────

function ProfilePanel({ datasetId, onActivationChanged }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [toggling, setToggling] = useState(false);
  const [toggleError, setToggleError] = useState(null);

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    fetchDatasetProfile(datasetId)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, [datasetId]);

  useEffect(() => { load(); }, [load]);

  const handleToggle = async () => {
    setToggling(true); setToggleError(null);
    try {
      if (state.data.activated) await deactivateDataset(datasetId);
      else await activateDataset(datasetId);
      load();
      onActivationChanged();
    } catch (e) {
      setToggleError(e.message);
    } finally {
      setToggling(false);
    }
  };

  if (state.loading) return <LoadingState label="Computing business profile…" rows={3} />;
  if (state.error) return <ErrorState message={state.error} onRetry={load} />;
  if (!state.data?.available) {
    return (
      <EmptyState icon="alert" title="Profile unavailable"
        description={state.data?.detail || "This dataset has not finished processing yet."} tone="muted" />
    );
  }

  const p = state.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "0.7rem",
        background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: "0.9rem 1.1rem",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.9rem", flexWrap: "wrap" }}>
          <Badge label={p.activated ? "Activated" : "Inactive"} color={p.activated ? "var(--ok)" : "var(--muted)"} dot />
          <Badge label={p.forecast_enabled ? "Forecast Enabled" : "No Time Axis"} color={p.forecast_enabled ? "var(--info)" : "var(--muted)"} />
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
            {p.monitorable_metrics.length} monitorable metric{p.monitorable_metrics.length !== 1 ? "s" : ""}
          </span>
        </div>
        <Button icon={p.activated ? "x" : "zap"} onClick={handleToggle} disabled={toggling}
          variant={p.activated ? "ghost" : "primary"}>
          {toggling ? "…" : p.activated ? "Deactivate" : "Activate"}
        </Button>
      </div>
      {toggleError && <div style={{ fontSize: "0.76rem", color: "var(--err)", fontFamily: "var(--font-mono)" }}>{toggleError}</div>}

      <div className="aeam-grid-auto">
        <Field label="Measures" value={p.measures.join(", ") || "—"} />
        <Field label="Dimensions" value={p.dimensions.join(", ") || "—"} />
        <Field label="Identifiers" value={p.identifiers.join(", ") || "—"} />
        <Field label="Time Axis" value={p.timestamp_column || "—"} mono />
      </div>

      <div>
        <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em",
          color: "var(--muted)", fontWeight: 700, marginBottom: "0.6rem" }}>Monitorable Metrics</div>
        <DataTable
          columns={[
            { key: "column", label: "Metric" },
            { key: "data_type", label: "Type", render: (r) => <span style={{ fontFamily: "var(--font-mono)" }}>{r.data_type}</span> },
            { key: "forecastable", label: "Forecast Candidate", render: (r) => (r.forecastable ? <Badge label="Yes" color="var(--info)" /> : "—") },
            { key: "rule_coverage", label: "Rule Coverage", render: (r) => (r.rule_coverage ? <Badge label="Governed" color="var(--ok)" /> : <Badge label="Statistical only" color="var(--warn)" />) },
            { key: "dimensions", label: "Sliced By", render: (r) => (r.dimensions || []).join(", ") || "—" },
          ]}
          rows={p.monitorable_metrics}
          rowKey={(m) => m.metric_id}
          empty="No monitorable metrics — this dataset has no metric columns."
        />
      </div>
    </div>
  );
}

// ─── Schema Explorer panel ───────────────────────────────────────────────────

function SchemaExplorerPanel({ columns }) {
  if (!columns || columns.length === 0) {
    return <EmptyState icon="layers" title="No schema recorded" description="This dataset has not been profiled yet." tone="muted" />;
  }
  return (
    <DataTable
      columns={[
        { key: "name", label: "Column" },
        { key: "role", label: "Role", render: (r) => <Badge label={r.role} color={
          r.role === "metric" ? "var(--ok)" : r.role === "timestamp" ? "var(--info)" : r.role === "identifier" ? "var(--warn)" : "var(--muted)"
        } /> },
        { key: "type", label: "Type", render: (r) => <span style={{ fontFamily: "var(--font-mono)" }}>{r.type}</span> },
        { key: "nullable", label: "Nullable", render: (r) => (r.nullable ? "Yes" : "No") },
        { key: "is_metric", label: "Metric", render: (r) => (r.is_metric ? <Badge label="Metric" color="var(--ok)" dot /> : "—") },
      ]}
      rows={columns}
      rowKey={(c) => c.name}
      empty="No columns recorded."
    />
  );
}

// ─── Dataset detail modal ────────────────────────────────────────────────────

function DatasetDetailModal({ datasetId, onClose, onChanged }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [tab, setTab] = useState("overview"); // overview | schema | profile | preview | versions
  const [reindexing, setReindexing] = useState(false);
  const [reindexError, setReindexError] = useState(null);

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    fetchDatasetDetail(datasetId)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, [datasetId]);

  useEffect(() => { load(); }, [load]);

  const handleReindex = async () => {
    setReindexing(true); setReindexError(null);
    try {
      const { job_id } = await reindexDataset(datasetId);
      await pollJob(job_id, () => {});
      load();
      onChanged();
    } catch (e) {
      setReindexError(e.message);
    } finally {
      setReindexing(false);
    }
  };

  const TABS = [
    ["overview", "Overview"],
    ["schema", "Schema Explorer"],
    ["profile", "Business Profile & Monitoring"],
    ["preview", "Preview"],
    ["versions", "Versions"],
  ];

  return (
    <Modal title="Dataset Details" icon="layers" onClose={onClose} maxWidth={920}>
      {state.loading && <LoadingState label="Loading dataset…" rows={4} />}
      {state.error && <ErrorState message={state.error} onRetry={load} />}
      {!state.loading && !state.error && state.data && (
        <div style={{ display: "flex", flexDirection: "column", gap: "1.1rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.6rem" }}>
            <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
              {TABS.map(([key, label]) => (
                <button key={key} onClick={() => setTab(key)} style={{
                  fontSize: "0.68rem", letterSpacing: "0.05em", textTransform: "uppercase",
                  background: tab === key ? "var(--accent-dim)" : "none",
                  border: `1px solid ${tab === key ? "var(--accent-border)" : "var(--border)"}`,
                  color: tab === key ? "var(--accent)" : "var(--muted)",
                  borderRadius: 6, padding: "0.32rem 0.65rem", cursor: "pointer", transition: "all 0.15s",
                }}>{label}</button>
              ))}
            </div>
            <Button icon="branch" onClick={handleReindex} disabled={reindexing}>
              {reindexing ? "Re-indexing…" : "Re-index"}
            </Button>
          </div>

          {reindexError && (
            <div style={{ fontSize: "0.76rem", color: "var(--err)", fontFamily: "var(--font-mono)" }}>{reindexError}</div>
          )}

          {tab === "overview" && (
            <div className="aeam-grid-auto">
              <Field label="Name" value={state.data.name} />
              <Field label="Source" value={state.data.source_name || "—"} />
              <Field label="File Type" value={state.data.file_type || "—"} mono />
              <Field label="Processing Status" value={<StatusBadge status={state.data.status} />} />
              <Field label="Row Count" value={state.data.row_count ?? 0} />
              <Field label="Column Count" value={(state.data.schema?.columns || []).length} />
              <Field label="Current Version" value={state.data.active_version?.version ?? "—"} />
              <Field label="Created" value={fmtTime(state.data.created_at)} title={state.data.created_at} />
              <Field label="Last Updated" value={fmtTime(state.data.last_ingested_at || state.data.created_at)}
                title={state.data.last_ingested_at || state.data.created_at} />
              <Field label="Content Hash" value={state.data.active_version?.content_hash}
                mono title={state.data.active_version?.content_hash} />
            </div>
          )}

          {tab === "schema" && <SchemaExplorerPanel columns={state.data.schema?.columns} />}

          {tab === "profile" && <ProfilePanel datasetId={datasetId} onActivationChanged={onChanged} />}

          {tab === "preview" && <PreviewPanel kind="dataset" id={datasetId} />}

          {tab === "versions" && <VersionHistoryTable parentType="dataset" parentId={datasetId} />}
        </div>
      )}
    </Modal>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function DataCenter() {
  const [datasets, setDatasets] = useState([]);
  const [activatedIds, setActivatedIds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [detailId, setDetailId] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [rowBusy, setRowBusy] = useState(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) { setLoading(true); setError(null); }
    try {
      const [ds, activation] = await Promise.all([fetchDatasets(), fetchActivation()]);
      setDatasets(Array.isArray(ds) ? ds : []);
      setActivatedIds(activation?.activated_dataset_ids || []);
      if (silent) setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const activatedSet = useMemo(() => new Set(activatedIds), [activatedIds]);
  const STATUS_FILTERS = ["ALL", "pending", "processing", "indexed", "error"];

  const needle = search.trim().toLowerCase();
  const filtered = useMemo(() => {
    let rows = datasets;
    if (statusFilter !== "ALL") rows = rows.filter((d) => d.status === statusFilter);
    if (needle) rows = rows.filter((d) => (d.name || "").toLowerCase().includes(needle));
    return rows;
  }, [datasets, statusFilter, needle]);

  const activatedCount = datasets.filter((d) => activatedSet.has(d.dataset_id)).length;
  const totalMetrics = datasets.reduce((sum, d) => sum + (d.metric_columns || []).length, 0);
  const isEmpty = !loading && !error && datasets.length === 0;

  const handleToggleActivation = async (dataset) => {
    setRowBusy(dataset.dataset_id);
    try {
      if (activatedSet.has(dataset.dataset_id)) await deactivateDataset(dataset.dataset_id);
      else await activateDataset(dataset.dataset_id);
      load(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setRowBusy(null);
    }
  };

  const handleRowReindex = async (dataset) => {
    setRowBusy(dataset.dataset_id);
    try {
      const { job_id } = await reindexDataset(dataset.dataset_id);
      await pollJob(job_id, () => {});
      load(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setRowBusy(null);
    }
  };

  const columns = [
    { key: "name", label: "Dataset" },
    { key: "source_name", label: "Source", render: (r) => r.source_name || "—" },
    { key: "file_type", label: "File Type", render: (r) => r.file_type || "—" },
    { key: "row_count", label: "Rows", align: "right" },
    { key: "metric_columns", label: "Metrics", align: "right", render: (r) => (r.metric_columns || []).length },
    { key: "status", label: "Status", render: (r) => <StatusBadge status={r.status} /> },
    {
      key: "activation", label: "Monitoring",
      render: (r) => (
        <Badge label={activatedSet.has(r.dataset_id) ? "Activated" : "Inactive"}
          color={activatedSet.has(r.dataset_id) ? "var(--ok)" : "var(--muted)"} dot />
      ),
    },
    { key: "last_ingested_at", label: "Last Updated", render: (r) => fmtRelative(r.last_ingested_at || r.created_at) },
    {
      key: "actions", label: "", align: "right",
      render: (r) => (
        <div style={{ display: "flex", gap: "0.4rem", justifyContent: "flex-end", flexWrap: "wrap" }}>
          <Button icon="search" onClick={() => setDetailId(r.dataset_id)}>View</Button>
          <Button icon={activatedSet.has(r.dataset_id) ? "x" : "zap"} disabled={rowBusy === r.dataset_id}
            onClick={() => handleToggleActivation(r)}>
            {rowBusy === r.dataset_id ? "…" : activatedSet.has(r.dataset_id) ? "Deactivate" : "Activate"}
          </Button>
          <Button icon="branch" disabled={rowBusy === r.dataset_id} onClick={() => handleRowReindex(r)}>Re-index</Button>
          <Button icon="x" onClick={() => setConfirmDelete(r)} style={{ color: "var(--err)" }}>Delete</Button>
        </div>
      ),
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        title="Data Center"
        subtitle="Activate registered datasets as live metric sources for the Monitor, Statistical, Forecast and Rule agents"
        right={<Button icon="activity" onClick={() => load()} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="Datasets" value={loading ? undefined : datasets.length} loading={loading} icon="layers" sub="registered datasets" />
        <MetricCard label="Activated" value={loading ? undefined : activatedCount} loading={loading} icon="zap" sub="feeding the monitor agent" />
        <MetricCard label="Inactive" value={loading ? undefined : datasets.length - activatedCount} loading={loading} icon="database" sub="registered but not monitored" />
        <MetricCard label="Metrics" value={loading ? undefined : totalMetrics} loading={loading} icon="branch" sub="total metric columns" />
      </div>

      {/* Same upload surface as Knowledge Center (components/pages/KnowledgeCenter.jsx's
          UploadDropzone, reused verbatim) — an operator can register a CSV/Excel
          dataset directly from Data Center instead of being told to go elsewhere. */}
      <UploadDropzone onUploadsSettled={() => load(true)} currentPage="data" />

      {error && (
        <div style={{ marginBottom: "1.4rem" }}>
          <ErrorState message={error} onRetry={() => load()} />
        </div>
      )}

      {loading && <LoadingState label="Loading the structured data layer…" rows={5} />}

      {isEmpty && (
        <EmptyState icon="layers" title="No datasets yet"
          description="Drop a CSV or Excel file above to register a dataset here." />
      )}

      {!loading && !error && !isEmpty && (
        <>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.9rem", flexWrap: "wrap", gap: "0.7rem" }}>
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              {STATUS_FILTERS.map((f) => {
                const active = statusFilter === f;
                return (
                  <button key={f} onClick={() => setStatusFilter(f)} style={{
                    fontSize: "0.7rem", letterSpacing: "0.06em", textTransform: "uppercase",
                    background: active ? "var(--accent-dim)" : "none",
                    border: `1px solid ${active ? "var(--accent-border)" : "var(--border)"}`,
                    color: active ? "var(--accent)" : "var(--muted)",
                    borderRadius: 6, padding: "0.3rem 0.75rem", cursor: "pointer", transition: "all 0.15s",
                  }}>{f}</button>
                );
              })}
            </div>
            <SearchBox value={search} onChange={setSearch} placeholder="Search datasets…" />
          </div>

          <Panel title="Dataset Catalog" icon="layers" pad={false}>
            <DataTable columns={columns} rows={filtered} rowKey={(r) => r.dataset_id}
              empty={needle || statusFilter !== "ALL" ? "No datasets match this filter." : "No datasets registered yet."} />
          </Panel>
        </>
      )}

      {detailId && (
        <DatasetDetailModal datasetId={detailId} onClose={() => setDetailId(null)} onChanged={() => load(true)} />
      )}
      {confirmDelete && (
        <ConfirmDeleteModal kind="dataset" item={confirmDelete} onClose={() => setConfirmDelete(null)}
          onDeleted={() => load(true)} />
      )}
    </PageContainer>
  );
}
