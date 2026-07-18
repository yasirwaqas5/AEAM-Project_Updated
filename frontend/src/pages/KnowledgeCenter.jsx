import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Link } from "react-router-dom";
import { PageHeader, Badge, Modal, Field, Skeleton, Button, Icon, fmtTime, fmtRelative } from "../components/ui";
import { PageContainer, MetricCard, Panel, DataTable, LoadingState, ErrorState, EmptyState } from "../components/library";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/KnowledgeCenter.jsx  (Enterprise Knowledge Center)
 *
 * Full interactive surface over the existing Enterprise Data Layer
 * (B1.1–B1.5) and its API (aeam/api/knowledge.py, aeam/api/ingest.py):
 * upload (drag & drop, progress, job polling, auto-refresh), documents,
 * datasets, search, preview, version history, re-index, and delete (with
 * an opt-in purge of Qdrant vectors / BlobStore bytes). Every request below
 * hits an existing or newly-exposed endpoint — no client-side business
 * logic beyond formatting/polling.
 *
 * `UploadDropzone` is exported and reused verbatim by pages/DataCenter.jsx
 * (same component mounted on both pages, not duplicated) — completed uploads
 * surface a "View in Knowledge Center / View in Data Center" link based on
 * the real `asset_type` the upload API already returns, so a document
 * uploaded from Data Center or a dataset uploaded from here can jump
 * straight to wherever it actually lives.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Data fetching (plain fetch, mirrors pages/Incidents.jsx's convention) ──

export async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = "";
    try { const body = await res.json(); detail = body?.detail ? ` — ${JSON.stringify(body.detail)}` : ""; } catch { /* ignore */ }
    throw new Error(`HTTP ${res.status}${detail} — ${url}`);
  }
  return res.status === 204 ? null : res.json();
}

const fetchDocuments = () => fetchJSON("/api/v1/knowledge/documents");
export const fetchDatasets = () => fetchJSON("/api/v1/knowledge/datasets");
const fetchJobs = () => fetchJSON("/api/v1/ingest/jobs?limit=20"); // existing B1.2 endpoint, reused as-is
const fetchJob = (jobId) => fetchJSON(`/api/v1/ingest/jobs/${jobId}`);
const fetchDocumentDetail = (id) => fetchJSON(`/api/v1/knowledge/documents/${id}`);
// Exported: reused verbatim by pages/DataCenter.jsx rather than duplicated
// (dataset preview, version history, delete-with-purge, and status colour
// are identical concerns in both Knowledge Center and Data Center).
export const fetchDatasetDetail = (id) => fetchJSON(`/api/v1/knowledge/datasets/${id}`);
const fetchDocumentPreview = (id) => fetchJSON(`/api/v1/knowledge/documents/${id}/preview`);
const fetchDocumentPolicies = (id) => fetchJSON(`/api/v1/knowledge/documents/${id}/policies`);
export const fetchDatasetPreview = (id) => fetchJSON(`/api/v1/knowledge/datasets/${id}/preview`);
export const fetchVersions = (parentType, parentId) =>
  fetchJSON(`/api/v1/knowledge/versions?parent_type=${parentType}&parent_id=${parentId}`);
const reindexDocument = (id) => fetchJSON(`/api/v1/knowledge/documents/${id}/reindex`, { method: "POST" });
export const reindexDataset = (id) => fetchJSON(`/api/v1/knowledge/datasets/${id}/reindex`, { method: "POST" });
const deleteDocument = (id, purge) =>
  fetchJSON(`/api/v1/knowledge/documents/${id}${purge ? "?purge=true" : ""}`, { method: "DELETE" });
export const deleteDataset = (id, purge) =>
  fetchJSON(`/api/v1/knowledge/datasets/${id}${purge ? "?purge=true" : ""}`, { method: "DELETE" });

/** Upload with real progress via XHR (fetch has no reliable upload-progress event). */
function uploadFileWithProgress(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/ingest/upload");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new Error(body?.detail ? JSON.stringify(body.detail) : `HTTP ${xhr.status}`));
      } catch {
        reject(new Error(`HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.send(form);
  });
}

/** Poll a job until it reaches a terminal state; calls onUpdate on every tick. */
export async function pollJob(jobId, onUpdate, { intervalMs = 1200, timeoutMs = 120000 } = {}) {
  const TERMINAL = new Set(["done", "failed", "cancelled"]);
  const start = Date.now();
  for (;;) {
    const job = await fetchJob(jobId);
    onUpdate(job);
    if (TERMINAL.has(job.status)) return job;
    if (Date.now() - start > timeoutMs) throw new Error("Timed out waiting for job to complete");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

// ─── Status → colour (page-local; ui.jsx/library.jsx untouched) ────────────

const STATUS_COLOR = {
  indexed: "var(--ok)", done: "var(--ok)",
  processing: "var(--info)", validating: "var(--info)", extracting: "var(--info)", indexing: "var(--info)",
  pending: "var(--warn)", queued: "var(--warn)", stale: "var(--warn)",
  error: "var(--err)", failed: "var(--err)",
  archived: "var(--muted)", deleted: "var(--muted)", cancelled: "var(--muted)",
};
export const statusColor = (s) => STATUS_COLOR[String(s ?? "").toLowerCase()] || "var(--muted)";

export function StatusBadge({ status }) {
  return <Badge label={status || "—"} color={statusColor(status)} dot />;
}

// ─── Search box (page-local; no shared input primitive exists yet) ─────────

export function SearchBox({ value, onChange, placeholder }) {
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

// ─── Upload dropzone ─────────────────────────────────────────────────────────
//
// The ONE upload surface for the whole app — mounted here in Knowledge Center
// and reused verbatim (not duplicated) by pages/DataCenter.jsx, since both
// are equally valid places an operator expects to be able to drop a file.
// `currentPage` ("knowledge" | "data") only controls whether a completed
// upload's cross-navigation link is shown — it never changes upload/validate/
// poll behaviour, which is identical everywhere this mounts.

const DESTINATION = {
  document: { to: "/knowledge", page: "knowledge", label: "View in Knowledge Center" },
  dataset:  { to: "/data",      page: "data",      label: "View in Data Center" },
};

export function UploadDropzone({ onUploadsSettled, currentPage = "knowledge" }) {
  const [dragging, setDragging] = useState(false);
  const [uploads, setUploads] = useState([]); // [{ id, name, progress, status, error, assetType }]
  const inputRef = useRef(null);
  const nextId = useRef(0);

  const startUpload = useCallback((file) => {
    const id = ++nextId.current;
    setUploads((prev) => [...prev, { id, name: file.name, progress: 0, status: "uploading", error: null, assetType: null }]);

    uploadFileWithProgress(file, (progress) => {
      setUploads((prev) => prev.map((u) => (u.id === id ? { ...u, progress } : u)));
    })
      .then((body) => {
        setUploads((prev) => prev.map((u) => (u.id === id
          ? { ...u, status: "processing", progress: 100, assetType: body.asset_type } : u)));
        return pollJob(body.job_id, (job) => {
          setUploads((prev) => prev.map((u) => (u.id === id
            ? { ...u, status: job.status, stage: job.stage } : u)));
        });
      })
      .then(() => {
        onUploadsSettled();
        // Drop the row after a while so the user has a fair chance to click
        // the cross-navigation link (if one is showing) before it vanishes.
        setTimeout(() => setUploads((prev) => prev.filter((u) => u.id !== id)), 6000);
      })
      .catch((e) => {
        setUploads((prev) => prev.map((u) => (u.id === id ? { ...u, status: "failed", error: e.message } : u)));
        onUploadsSettled();
      });
  }, [onUploadsSettled]);

  const handleFiles = useCallback((fileList) => {
    Array.from(fileList || []).forEach(startUpload);
  }, [startUpload]);

  return (
    <div style={{ marginBottom: "1.4rem" }}>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); handleFiles(e.dataTransfer.files); }}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        style={{
          border: `1.5px dashed ${dragging ? "var(--accent)" : "var(--border)"}`,
          borderRadius: 12, padding: "1.6rem", textAlign: "center", cursor: "pointer",
          background: dragging ? "var(--accent-dim)" : "var(--surface)",
          transition: "all 0.15s",
        }}
      >
        <input ref={inputRef} type="file" multiple hidden
          onChange={(e) => { handleFiles(e.target.files); e.target.value = ""; }} />
        <Icon name="zap" size={22} color={dragging ? "var(--accent)" : "var(--muted)"} />
        <div style={{ marginTop: "0.6rem", fontSize: "0.85rem", color: "var(--text)", fontWeight: 600 }}>
          Drag & drop files here, or click to browse
        </div>
        <div style={{ marginTop: "0.25rem", fontSize: "0.7rem", color: "var(--muted)" }}>
          Documents (PDF, DOCX, Markdown, JSON, XML, logs) and datasets (CSV, Excel)
        </div>
      </div>

      {uploads.length > 0 && (
        <div style={{ marginTop: "0.8rem", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {uploads.map((u) => {
            const destination = u.status === "done" && DESTINATION[u.assetType];
            const showLink = destination && destination.page !== currentPage;
            return (
              <div key={u.id} style={{
                display: "flex", alignItems: "center", gap: "0.75rem",
                background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 9,
                padding: "0.55rem 0.85rem", fontSize: "0.76rem", flexWrap: "wrap",
              }}>
                <Icon name={u.status === "failed" ? "alert" : u.status === "done" ? "check" : "activity"}
                  size={14} color={u.status === "failed" ? "var(--err)" : u.status === "done" ? "var(--ok)" : "var(--info)"} />
                <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {u.name}
                </span>
                {u.status === "uploading" && (
                  <div style={{ width: 120, height: 5, background: "var(--border)", borderRadius: 3, overflow: "hidden" }}>
                    <div style={{ width: `${u.progress}%`, height: "100%", background: "var(--info)", transition: "width 0.2s" }} />
                  </div>
                )}
                <span style={{ color: statusColor(u.status), fontFamily: "var(--font-mono)", minWidth: 70, textAlign: "right" }}>
                  {u.status === "uploading" ? `${u.progress}%` : (u.stage || u.status)}
                </span>
                {u.error && <span style={{ color: "var(--err)", maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis" }} title={u.error}>{u.error}</span>}
                {showLink && (
                  <Link to={destination.to} style={{
                    fontSize: "0.72rem", color: "var(--accent)", fontWeight: 600, whiteSpace: "nowrap",
                    display: "flex", alignItems: "center", gap: "0.3rem",
                  }}>
                    {destination.label} <Icon name="chevron" size={11} style={{ transform: "rotate(-90deg)" }} />
                  </Link>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Confirm-delete modal ────────────────────────────────────────────────────

export function ConfirmDeleteModal({ kind, item, onClose, onDeleted }) {
  const [purge, setPurge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const label = kind === "document" ? (item.title || item.doc_id) : (item.name || item.dataset_id);

  const handleDelete = async () => {
    setBusy(true); setError(null);
    try {
      const id = kind === "document" ? item.doc_id : item.dataset_id;
      if (kind === "document") await deleteDocument(id, purge); else await deleteDataset(id, purge);
      onDeleted();
      onClose();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <Modal title={`Delete ${kind === "document" ? "Document" : "Dataset"}`} icon="alert" onClose={onClose} maxWidth={480}>
      <p style={{ fontSize: "0.85rem", color: "var(--text)", margin: "0 0 1rem" }}>
        Delete <strong>{label}</strong>? This removes it from the Knowledge Center.
      </p>
      <label style={{ display: "flex", alignItems: "flex-start", gap: "0.6rem", fontSize: "0.78rem", color: "var(--muted)", marginBottom: "1.2rem", cursor: "pointer" }}>
        <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} style={{ marginTop: 2 }} />
        <span>
          Also purge {kind === "document" ? "Qdrant vectors and " : ""}BlobStore bytes.
          Skipped automatically if another document or dataset still shares the same content.
        </span>
      </label>
      {error && (
        <div style={{ marginBottom: "1rem", fontSize: "0.76rem", color: "var(--err)", fontFamily: "var(--font-mono)" }}>
          {error}
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.6rem" }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="primary" icon="x" onClick={handleDelete} disabled={busy}
          style={{ color: "var(--err)", borderColor: "rgba(255,95,87,0.4)", background: "rgba(255,95,87,0.08)" }}>
          {busy ? "Deleting…" : "Delete"}
        </Button>
      </div>
    </Modal>
  );
}

// ─── Preview panel (inside detail modal) ────────────────────────────────────

export function PreviewPanel({ kind, id }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    const fetcher = kind === "document" ? fetchDocumentPreview : fetchDatasetPreview;
    fetcher(id)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, [kind, id]);

  useEffect(() => { load(); }, [load]);

  if (state.loading) return <Skeleton height={100} />;
  if (state.error) return <ErrorState message={state.error} onRetry={load} />;
  if (!state.data?.available) {
    return (
      <EmptyState icon="alert" title="Preview unavailable"
        description={state.data?.detail || "This content type cannot be previewed yet."} tone="muted" />
    );
  }

  if (kind === "document") {
    return (
      <div>
        <pre className="aeam-json" style={{ maxHeight: 320, whiteSpace: "pre-wrap" }}>{state.data.text}</pre>
        {state.data.truncated && (
          <div style={{ marginTop: "0.5rem", fontSize: "0.68rem", color: "var(--muted)" }}>
            Showing first {state.data.text.length.toLocaleString()} of {state.data.char_count.toLocaleString()} characters.
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <DataTable
        columns={state.data.columns.map((c) => ({ key: c, label: c }))}
        rows={state.data.rows}
        rowKey={(_, i) => i}
        empty="No rows to preview."
      />
      <div style={{ marginTop: "0.5rem", fontSize: "0.68rem", color: "var(--muted)" }}>
        Showing {state.data.previewed_rows} of {state.data.total_rows.toLocaleString()} row(s).
      </div>
    </div>
  );
}

// ─── Policy panel (Phase C2 — Enterprise Policy Intelligence Engine) ────────
//
// Renders the structured business policies extracted from THIS document —
// additional structured knowledge, never a replacement for the document
// itself. Deliberately its own tab, never merged into Preview: a policy is a
// distinct kind of artifact (condition/actions/department/etc.), not raw
// document text.

export function PolicyPanel({ id }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    fetchDocumentPolicies(id)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, [id]);

  useEffect(() => { load(); }, [load]);

  if (state.loading) return <Skeleton height={100} />;
  if (state.error) return <ErrorState message={state.error} onRetry={load} />;

  const policies = state.data?.policies || [];
  if (policies.length === 0) {
    return (
      <EmptyState icon="shield" title="No policies extracted"
        description="This document contains no recognizable business rule (condition/threshold/escalation/approval requirement) — an honest outcome, not every document is a policy document."
        tone="muted" />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
        {policies.length} polic{policies.length !== 1 ? "ies" : "y"} extracted from this document
      </div>
      {policies.map((p) => (
        <div key={p.policy_id} style={{
          border: "1px solid var(--border)", borderRadius: 10,
          background: "rgba(255,255,255,0.015)", padding: "0.85rem 1rem",
          display: "flex", flexDirection: "column", gap: "0.6rem",
        }}>
          {p.business_rule && (
            <div style={{ fontSize: "0.82rem", color: "var(--text)", fontWeight: 600 }}>{p.business_rule}</div>
          )}
          <div style={{ fontSize: "0.76rem", color: "var(--muted)", fontStyle: "italic", lineHeight: 1.45 }}>
            "{p.raw_text}"
          </div>
          <div className="aeam-grid-auto" style={{ gap: "0.7rem" }}>
            {p.condition && <Field label="Condition" value={p.condition} mono />}
            {p.threshold && <Field label="Threshold" value={p.threshold} mono />}
            {p.actions?.length > 0 && <Field label="Actions" value={p.actions.join(", ")} />}
            {p.escalation_rule && <Field label="Escalation" value={p.escalation_rule} />}
            {p.approval_required != null && <Field label="Approval Required" value={p.approval_required ? "Yes" : "No"} />}
            {p.department && <Field label="Department" value={p.department} />}
            {p.role && <Field label="Role" value={p.role} />}
            {p.time_constraint && <Field label="Time Constraint" value={p.time_constraint} />}
            {p.priority && <Field label="Priority" value={<Badge label={p.priority} color={
              p.priority === "critical" || p.priority === "high" ? "var(--err)" : p.priority === "medium" ? "var(--warn)" : "var(--muted)"
            } />} />}
            {p.related_metrics?.length > 0 && <Field label="Related Metrics" value={p.related_metrics.join(", ")} mono />}
          </div>
          <div style={{ display: "flex", gap: "1rem", fontSize: "0.66rem", color: "var(--muted)", paddingTop: "0.4rem", borderTop: "1px solid var(--border)" }}>
            <span>Source: {p.source_document || "—"}</span>
            {p.source_chunk && <span title={p.source_chunk}>Chunk: {p.source_chunk.slice(0, 12)}…</span>}
            <span>{fmtTime(p.extracted_at)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Detail modal ────────────────────────────────────────────────────────────

function DetailModal({ kind, id, onClose, onChanged }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [tab, setTab] = useState("overview"); // 'overview' | 'preview' | 'versions'
  const [reindexing, setReindexing] = useState(false);
  const [reindexError, setReindexError] = useState(null);

  const load = useCallback(() => {
    setState({ loading: true, error: null, data: null });
    const fetcher = kind === "document" ? fetchDocumentDetail : fetchDatasetDetail;
    fetcher(id)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((e) => setState({ loading: false, error: e.message, data: null }));
  }, [kind, id]);

  useEffect(() => { load(); }, [load]);

  const handleReindex = async () => {
    setReindexing(true); setReindexError(null);
    try {
      const { job_id } = kind === "document" ? await reindexDocument(id) : await reindexDataset(id);
      await pollJob(job_id, () => {});
      load();
      onChanged();
    } catch (e) {
      setReindexError(e.message);
    } finally {
      setReindexing(false);
    }
  };

  const title = kind === "document" ? "Document Details" : "Dataset Details";
  const parentType = kind === "document" ? "document" : "dataset";

  return (
    <Modal title={title} icon="database" onClose={onClose} maxWidth={860}>
      {state.loading && <LoadingState label="Loading details…" rows={4} />}
      {state.error && <ErrorState message={state.error} onRetry={load} />}
      {!state.loading && !state.error && state.data && (
        <div style={{ display: "flex", flexDirection: "column", gap: "1.1rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.6rem" }}>
            <div style={{ display: "flex", gap: "0.4rem" }}>
              {[
                ["overview", "Overview"], ["preview", "Preview"],
                ...(kind === "document" ? [["policies", "Policies"]] : []),
                ["versions", "Versions"],
              ].map(([key, label]) => (
                <button key={key} onClick={() => setTab(key)} style={{
                  fontSize: "0.7rem", letterSpacing: "0.06em", textTransform: "uppercase",
                  background: tab === key ? "var(--accent-dim)" : "none",
                  border: `1px solid ${tab === key ? "var(--accent-border)" : "var(--border)"}`,
                  color: tab === key ? "var(--accent)" : "var(--muted)",
                  borderRadius: 6, padding: "0.32rem 0.75rem", cursor: "pointer", transition: "all 0.15s",
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
            <>
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
                    color: "var(--muted)", fontWeight: 700, marginBottom: "0.6rem", marginTop: "1.1rem" }}>Inferred Schema</div>
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
            </>
          )}

          {tab === "preview" && <PreviewPanel kind={kind} id={id} />}

          {tab === "policies" && kind === "document" && <PolicyPanel id={id} />}

          {tab === "versions" && <VersionHistoryTable parentType={parentType} parentId={id} />}
        </div>
      )}
    </Modal>
  );
}

export function VersionHistoryTable({ parentType, parentId }) {
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

const AUTO_REFRESH_MS = 15000;

export default function KnowledgeCenter() {
  const [documents, setDocuments] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null); // { kind: 'document'|'dataset', id }
  const [confirmDelete, setConfirmDelete] = useState(null); // { kind, item }
  const [rowBusy, setRowBusy] = useState(null); // id currently re-indexing from the table row

  const load = useCallback(async (silent = false) => {
    if (!silent) { setLoading(true); setError(null); }
    try {
      const [docs, ds, jobList] = await Promise.all([fetchDocuments(), fetchDatasets(), fetchJobs()]);
      setDocuments(Array.isArray(docs) ? docs : []);
      setDatasets(Array.isArray(ds) ? ds : []);
      setJobs(Array.isArray(jobList) ? jobList : []);
      if (silent) setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Automatic background refresh — picks up ingestion jobs progressing
  // outside this tab (e.g. re-index) without requiring a manual click.
  useEffect(() => {
    const timer = setInterval(() => load(true), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

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
  const isEmpty = !loading && !error && documents.length === 0 && datasets.length === 0;

  const handleRowReindex = async (kind, id) => {
    setRowBusy(id);
    try {
      const { job_id } = kind === "document" ? await reindexDocument(id) : await reindexDataset(id);
      await pollJob(job_id, () => {});
      load(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setRowBusy(null);
    }
  };

  const documentColumns = [
    { key: "title", label: "Document" },
    { key: "source_name", label: "Source", render: (r) => r.source_name || "—" },
    { key: "file_type", label: "File Type", render: (r) => r.file_type || "—" },
    { key: "chunk_count", label: "Chunks", align: "right" },
    { key: "status", label: "Embedding Status", render: (r) => <StatusBadge status={r.status} /> },
    { key: "updated_at", label: "Last Updated", render: (r) => fmtRelative(r.updated_at) },
    {
      key: "actions", label: "", align: "right",
      render: (r) => (
        <div style={{ display: "flex", gap: "0.4rem", justifyContent: "flex-end" }}>
          <Button icon="search" onClick={() => setDetail({ kind: "document", id: r.doc_id })}>View</Button>
          <Button icon="branch" disabled={rowBusy === r.doc_id} onClick={() => handleRowReindex("document", r.doc_id)}>
            {rowBusy === r.doc_id ? "…" : "Re-index"}
          </Button>
          <Button icon="x" onClick={() => setConfirmDelete({ kind: "document", item: r })}
            style={{ color: "var(--err)" }}>Delete</Button>
        </div>
      ),
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
      render: (r) => (
        <div style={{ display: "flex", gap: "0.4rem", justifyContent: "flex-end" }}>
          <Button icon="search" onClick={() => setDetail({ kind: "dataset", id: r.dataset_id })}>View</Button>
          <Button icon="branch" disabled={rowBusy === r.dataset_id} onClick={() => handleRowReindex("dataset", r.dataset_id)}>
            {rowBusy === r.dataset_id ? "…" : "Re-index"}
          </Button>
          <Button icon="x" onClick={() => setConfirmDelete({ kind: "dataset", item: r })}
            style={{ color: "var(--err)" }}>Delete</Button>
        </div>
      ),
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
        subtitle="Upload, inspect, and manage the documents and datasets the retrieval and monitoring agents depend on"
        right={<Button icon="activity" onClick={() => load()} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      <UploadDropzone onUploadsSettled={() => load(true)} currentPage="knowledge" />

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
              empty="No uploads yet — drop a file above to get started." />
          </Panel>
        </div>
      )}

      {error && (
        <div style={{ marginBottom: "1.4rem" }}>
          <ErrorState message={error} onRetry={() => load()} />
        </div>
      )}

      {loading && <LoadingState label="Loading the knowledge layer…" rows={5} />}

      {isEmpty && (
        <EmptyState
          icon="database"
          title="No knowledge yet"
          description="Drop a file into the upload area above — documents become searchable evidence for RAG, datasets become monitorable metrics."
        />
      )}

      {!loading && !error && !isEmpty && (
        <>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "0.9rem" }}>
            <SearchBox value={search} onChange={setSearch} placeholder="Search documents & datasets…" />
          </div>

          <div style={{ marginBottom: "1.4rem" }}>
            <Panel title="Documents" icon="database" pad={false}>
              <DataTable columns={documentColumns} rows={filteredDocuments} rowKey={(r) => r.doc_id}
                empty={needle ? `No documents match "${search}".` : "No documents ingested yet."} />
            </Panel>
          </div>

          <div>
            <Panel title="Datasets" icon="layers" pad={false}>
              <DataTable columns={datasetColumns} rows={filteredDatasets} rowKey={(r) => r.dataset_id}
                empty={needle ? `No datasets match "${search}".` : "No datasets registered yet."} />
            </Panel>
          </div>
        </>
      )}

      {detail && (
        <DetailModal kind={detail.kind} id={detail.id} onClose={() => setDetail(null)} onChanged={() => load(true)} />
      )}
      {confirmDelete && (
        <ConfirmDeleteModal
          kind={confirmDelete.kind}
          item={confirmDelete.item}
          onClose={() => setConfirmDelete(null)}
          onDeleted={() => load(true)}
        />
      )}
    </PageContainer>
  );
}
