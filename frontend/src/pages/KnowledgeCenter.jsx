import { useState, useEffect, useCallback, useMemo } from "react";
import { PageHeader, Badge, Modal, Field, Skeleton, Button, Icon, fmtTime, fmtRelative } from "../components/ui";
import { PageContainer, MetricCard, Panel, DataTable, LoadingState, ErrorState } from "../components/library";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/KnowledgeCenter.jsx  (Phase B1.6)
 *
 * Read-only surface over the existing Enterprise Data Layer (B1.1–B1.5):
 * documents (aeam/api/knowledge.py: /documents), datasets (/datasets, with
 * their inferred schema), recent ingestion jobs (reuses the EXISTING
 * /api/v1/ingest/jobs endpoint — not duplicated here), and per-item version
 * history (/versions). No upload, no re-index — those are explicitly out of
 * scope for this phase.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Data fetching (plain fetch, mirrors pages/Incidents.jsx's convention) ──

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  return res.json();
}

const fetchDocuments = () => fetchJSON("/api/v1/knowledge/documents");
const fetchDatasets = () => fetchJSON("/api/v1/knowledge/datasets");
const fetchJobs = () => fetchJSON("/api/v1/ingest/jobs?limit=20"); // existing B1.2 endpoint, reused as-is
const fetchDocumentDetail = (id) => fetchJSON(`/api/v1/knowledge/documents/${id}`);
const fetchDatasetDetail = (id) => fetchJSON(`/api/v1/knowledge/datasets/${id}`);
const fetchVersions = (parentType, parentId) =>
  fetchJSON(`/api/v1/knowledge/versions?parent_type=${parentType}&parent_id=${parentId}`);

// ─── Status → colour (page-local; ui.jsx/library.jsx untouched) ────────────

const STATUS_COLOR = {
  indexed: "var(--ok)", done: "var(--ok)",
  processing: "var(--info)", validating: "var(--info)", extracting: "var(--info)", indexing: "var(--info)",
  pending: "var(--warn)", queued: "var(--warn)", stale: "var(--warn)",
  error: "var(--err)", failed: "var(--err)",
  archived: "var(--muted)", deleted: "var(--muted)", cancelled: "var(--muted)",
};
const statusColor = (s) => STATUS_COLOR[String(s ?? "").toLowerCase()] || "var(--muted)";

function StatusBadge({ status }) {
  return <Badge label={status || "—"} color={statusColor(status)} dot />;
}

// ─── Search box (page-local; no shared input primitive exists yet) ─────────

function SearchBox({ value, onChange, placeholder }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: "0.5rem", minWidth: 220,
      background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 9,
      padding: "0.4rem 0.7rem",
    }}>
      <Icon name="search" size={13} color="var(--muted)" />
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        style={{
          flex: 1, background: "none", border: "none", outline: "none",
          color: "var(--text)", fontSize: "0.78rem", fontFamily: "var(--font-body)", minWidth: 0,
        }}
      />
      {value && (
        <button onClick={() => onChange("")} aria-label="Clear search"
          style={{ background: "none", border: "none", color: "var(--muted)", cursor: "pointer", display: "flex" }}>
          <Icon name="x" size={12} />
        </button>
      )}
    </div>
  );
}

// ─── Detail modal ────────────────────────────────────────────────────────────

function DetailModal({ kind, id, onClose }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    const fetcher = kind === "document" ? fetchDocumentDetail : fetchDatasetDetail;
    fetcher(id)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, [kind, id]);

  useEffect(() => { load(); }, [load]);

  const title = kind === "document" ? "Document Details" : "Dataset Details";
  const parentType = kind === "document" ? "document" : "dataset";

  return (
    <Modal title={title} icon="database" onClose={onClose} maxWidth={820}>
      {state.loading && <LoadingState label="Loading details…" rows={4} />}
      {state.error && <ErrorState message={state.error} onRetry={load} />}
      {!state.loading && !state.error && state.data && (
        <div style={{ display: "flex", flexDirection: "column", gap: "1.3rem" }}>
          <div className="aeam-grid-auto">
            <Field label={kind === "document" ? "Title" : "Name"} value={state.data.title ?? state.data.name} />
            <Field label="Source" value={state.data.source_name || "—"} />
            <Field label="File Type" value={state.data.file_type || "—"} mono />
            <Field label={kind === "document" ? "Embedding Status" : "Processing Status"}
              value={<StatusBadge status={state.data.status} />} />
            {kind === "document" ? (
              <Field label="Chunk Count" value={state.data.chunk_count ?? 0} />
            ) : (
              <>
                <Field label="Row Count" value={state.data.row_count ?? 0} />
                <Field label="Metric Columns"
                  value={(state.data.metric_columns || []).join(", ") || "—"} />
              </>
            )}
            <Field label="Created" value={fmtTime(state.data.created_at)} title={state.data.created_at} />
            <Field label="Last Updated"
              value={fmtTime(state.data.updated_at || state.data.last_ingested_at)}
              title={state.data.updated_at || state.data.last_ingested_at} />
            <Field label="Content Hash" value={state.data.active_version?.content_hash || state.data.content_hash}
              mono title={state.data.active_version?.content_hash || state.data.content_hash} />
          </div>

          {kind === "dataset" && state.data.schema && (
            <div>
              <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em",
                color: "var(--muted)", fontWeight: 700, marginBottom: "0.6rem" }}>Inferred Schema</div>
              <DataTable
                columns={[
                  { key: "name", label: "Column" },
                  { key: "type", label: "Type" },
                  { key: "role", label: "Role" },
                  { key: "is_metric", label: "Metric", render: (r) => (r.is_metric ? "Yes" : "—") },
                  { key: "nullable", label: "Nullable", render: (r) => (r.nullable ? "Yes" : "No") },
                ]}
                rows={state.data.schema.columns || []}
                rowKey={(c) => c.name}
                empty="No columns recorded."
              />
            </div>
          )}

          <div>
            <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em",
              color: "var(--muted)", fontWeight: 700, marginBottom: "0.6rem" }}>Version History</div>
            <VersionHistoryTable parentType={parentType} parentId={id} />
          </div>
        </div>
      )}
    </Modal>
  );
}

function VersionHistoryTable({ parentType, parentId }) {
  const [state, setState] = useState({ loading: true, error: null, versions: [] });

  const load = useCallback(() => {
    setState({ loading: true, error: null, versions: [] });
    fetchVersions(parentType, parentId)
      .then((versions) => setState({ loading: false, error: null, versions }))
      .catch((e) => setState({ loading: false, error: e.message, versions: [] }));
  }, [parentType, parentId]);

  useEffect(() => { load(); }, [load]);

  if (state.loading) return <Skeleton height={60} />;
  if (state.error) return <ErrorState message={state.error} onRetry={load} />;

  return (
    <DataTable
      columns={[
        { key: "version", label: "Ver.", align: "right" },
        { key: "is_active", label: "Active", render: (r) => (r.is_active ? <Badge label="Active" color="var(--ok)" dot /> : "—") },
        { key: "chunk_count", label: "Chunks", align: "right" },
        { key: "created_at", label: "Created", render: (r) => fmtTime(r.created_at) },
      ]}
      rows={state.versions}
      rowKey={(v) => v.version_id}
      empty="No versions recorded yet."
    />
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function KnowledgeCenter() {
  const [documents, setDocuments] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null); // { kind: 'document'|'dataset', id }

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [docs, ds, jobList] = await Promise.all([fetchDocuments(), fetchDatasets(), fetchJobs()]);
      setDocuments(Array.isArray(docs) ? docs : []);
      setDatasets(Array.isArray(ds) ? ds : []);
      setJobs(Array.isArray(jobList) ? jobList : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const needle = search.trim().toLowerCase();
  const filteredDocuments = useMemo(
    () => (needle ? documents.filter((d) => (d.title || "").toLowerCase().includes(needle)) : documents),
    [documents, needle],
  );
  const filteredDatasets = useMemo(
    () => (needle ? datasets.filter((d) => (d.name || "").toLowerCase().includes(needle)) : datasets),
    [datasets, needle],
  );
  const recentJobs = useMemo(
    () => [...jobs].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 8),
    [jobs],
  );

  const totalChunks = documents.reduce((sum, d) => sum + (d.chunk_count || 0), 0);
  const sourceCount = new Set(
    [...documents, ...datasets].map((r) => r.source_name).filter(Boolean),
  ).size;

  const documentColumns = [
    { key: "title", label: "Document" },
    { key: "source_name", label: "Source", render: (r) => r.source_name || "—" },
    { key: "file_type", label: "File Type", render: (r) => r.file_type || "—" },
    { key: "chunk_count", label: "Chunks", align: "right" },
    { key: "status", label: "Embedding Status", render: (r) => <StatusBadge status={r.status} /> },
    { key: "updated_at", label: "Last Updated", render: (r) => fmtRelative(r.updated_at) },
    {
      key: "actions", label: "", align: "right",
      render: (r) => <Button icon="search" onClick={() => setDetail({ kind: "document", id: r.doc_id })}>View</Button>,
    },
  ];

  const datasetColumns = [
    { key: "name", label: "Dataset" },
    { key: "source_name", label: "Source", render: (r) => r.source_name || "—" },
    { key: "file_type", label: "File Type", render: (r) => r.file_type || "—" },
    { key: "row_count", label: "Rows", align: "right" },
    { key: "metric_columns", label: "Metrics", render: (r) => (r.metric_columns || []).length },
    { key: "status", label: "Processing Status", render: (r) => <StatusBadge status={r.status} /> },
    { key: "created_at", label: "Last Updated", render: (r) => fmtRelative(r.last_ingested_at || r.created_at) },
    {
      key: "actions", label: "", align: "right",
      render: (r) => <Button icon="search" onClick={() => setDetail({ kind: "dataset", id: r.dataset_id })}>View</Button>,
    },
  ];

  const jobColumns = [
    { key: "filename", label: "File", render: (r) => r.filename || r.stage || "—" },
    { key: "category", label: "Category", render: (r) => r.category || "—" },
    { key: "status", label: "Status", render: (r) => <StatusBadge status={r.status} /> },
    { key: "progress", label: "Progress", align: "right", render: (r) => `${r.progress ?? 0}%` },
    { key: "created_at", label: "Uploaded", render: (r) => fmtRelative(r.created_at) },
  ];

  return (
    <PageContainer>
      <PageHeader
        title="Knowledge Center"
        subtitle="Documents, datasets and versions the retrieval and monitoring agents depend on"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="Documents" value={loading ? undefined : documents.length} loading={loading} icon="database" sub="registered documents" />
        <MetricCard label="Datasets" value={loading ? undefined : datasets.length} loading={loading} icon="layers" sub="registered datasets" />
        <MetricCard label="Chunks" value={loading ? undefined : totalChunks} loading={loading} icon="branch" sub="in vector index" />
        <MetricCard label="Sources" value={loading ? undefined : sourceCount} loading={loading} icon="target" sub="connected origins" />
      </div>

      {!loading && !error && (
        <div style={{ marginBottom: "1.4rem" }}>
          <Panel title="Recent uploads" icon="activity" pad={false}
            right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
              from the ingestion queue
            </span>}>
            <DataTable columns={jobColumns} rows={recentJobs} rowKey={(r) => r.job_id}
              empty="No uploads yet." />
          </Panel>
        </div>
      )}

      {error && (
        <div style={{ marginBottom: "1.4rem" }}>
          <ErrorState message={error} onRetry={load} />
        </div>
      )}

      {loading && <LoadingState label="Loading the knowledge layer…" rows={5} />}

      {!loading && !error && (
        <>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "0.9rem" }}>
            <SearchBox value={search} onChange={setSearch} placeholder="Search documents & datasets…" />
          </div>

          <div style={{ marginBottom: "1.4rem" }}>
            <Panel title="Documents" icon="database" pad={false}>
              <DataTable columns={documentColumns} rows={filteredDocuments} rowKey={(r) => r.doc_id}
                empty={needle ? `No documents match "${search}".` : "No documents ingested yet — upload a file to get started."} />
            </Panel>
          </div>

          <div>
            <Panel title="Datasets" icon="layers" pad={false}>
              <DataTable columns={datasetColumns} rows={filteredDatasets} rowKey={(r) => r.dataset_id}
                empty={needle ? `No datasets match "${search}".` : "No datasets registered yet — upload a CSV or Excel file to get started."} />
            </Panel>
          </div>
        </>
      )}

      {detail && <DetailModal kind={detail.kind} id={detail.id} onClose={() => setDetail(null)} />}
    </PageContainer>
  );
}
