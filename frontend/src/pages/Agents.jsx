import { useEffect, useState, useCallback } from "react";
import { UIStyles, PageHeader, Card, Icon, Skeleton } from "../components/ui";
import AgentLogCard from "../components/AgentLogCard";

// ─── Data fetching (API contract unchanged) ──────────────────────────────────

async function fetchLogs() {
  const res = await fetch("/api/v1/logs/agents");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function CardSkeleton() {
  return (
    <Card style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <Skeleton width={120} height={16} />
        <Skeleton width={80} height={22} style={{ borderRadius: 20 }} />
      </div>
      <Skeleton width="60%" height={12} />
      <div className="aeam-grid-auto"><Skeleton height={30} /><Skeleton height={30} /><Skeleton height={30} /><Skeleton height={30} /></div>
    </Card>
  );
}

export default function Agents() {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const data = await fetchLogs();
      setLogs(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <UIStyles />
      <div className="aeam-page">
        <PageHeader
          title="Agent Logs"
          subtitle="Recent agent execution audit trail"
          right={<button className="aeam-btn aeam-btn-ghost" onClick={load} disabled={loading}><Icon name="activity" size={13} />{loading ? "Loading…" : "Refresh"}</button>}
        />

        {error && (
          <div style={{ background: "rgba(255,95,87,0.08)", border: "1px solid rgba(255,95,87,0.25)", borderRadius: 10, padding: "1rem 1.25rem", color: "#ff5f57", fontSize: "0.8rem", fontFamily: "var(--font-mono)" }}>
            ⚠ Failed to load agent logs: {error}
          </div>
        )}

        {loading && (
          <div className="aeam-grid-2">{[1, 2, 3, 4].map((i) => <CardSkeleton key={i} />)}</div>
        )}

        {!loading && !error && logs.length === 0 && (
          <div style={{ border: "1px dashed var(--border)", borderRadius: 12, padding: "3rem", textAlign: "center", color: "var(--muted)", fontSize: "0.82rem" }}>
            No agent executions logged yet.
          </div>
        )}

        {!loading && !error && logs.length > 0 && (
          <div className="aeam-grid-2">
            {logs.map((log, i) => <AgentLogCard key={i} log={log} />)}
          </div>
        )}
      </div>
    </>
  );
}
