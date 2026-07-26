# AEAM Autonomous Operations (Phase E7)

Operator runbook for the autonomous detection loop, worker supervision,
and retrieval-index freshness. This document is the E7 deliverable
referenced by `ROADMAP.md`'s "Documentation updates" line for that phase.

---

## 1. What "autonomous" means in AEAM

There is no separate scheduler process. Autonomy is `MonitorAgent`'s own
polling loop (`aeam/agents/monitor/monitor_agent.py::start()`), which:

1. Polls the configured KPI sources every `MONITOR_INTERVAL_SECONDS`.
2. Applies deterministic detection (rule + statistical + forecast).
3. Publishes a confirmed `Event` onto the `EventBus`.
4. The `Orchestrator` (registered as the `"ALL"` wildcard handler) picks
   it up and drives the full investigation — with **zero** manual
   `POST /api/v1/trigger` call.

An APScheduler stub existed early in the project's history, was removed
in Phase E1 as dead code (it never ran and only published a synthetic
hardcoded event), and is **not** reintroduced here. `MonitorAgent`'s
loop *is* the scheduler — a second one would violate ENG-6 (one
mechanism, not two).

## 2. The gate: `ENABLE_MONITOR_AGENT`

Before Phase E7, the gating condition was:

```python
if settings.ENABLE_MONITOR_AGENT or settings.ENVIRONMENT != "production":
```

This was dishonest in **both** directions:

- In `production`, the loop only ran if the flag was explicitly set —
  and no shipped deployment artifact set it, so the flagship "24/7
  autonomous platform" claim was false in the one environment that
  mattered (2026-07 audit gate #4).
- In every non-production environment (`development`, `staging`,
  `test`), the loop ran **unconditionally**, regardless of the flag —
  an environment-based backdoor around the flag's own semantics.

Phase E7 makes the flag the **sole** authority, in every environment:

```python
if settings.ENABLE_MONITOR_AGENT:
```

### Environment posture matrix

| Environment | `ENABLE_MONITOR_AGENT` | Where it's set | Why |
|---|---|---|---|
| Local dev (`docker-compose.yml`) | `true` (default, overridable) | `docker-compose.yml` env block | Reproduces the always-on demo behaviour developers expect; override in `.env` to test trigger-only flows. |
| CI / automated tests | unset → `false` (Settings default) | n/a — tests construct their own fixtures, never the real app | No test relies on the real background loop; each test builds a minimal component or app directly. |
| Staging | `true` (recommended) | operator's staging env config | Staging should mirror production's autonomy posture to catch detection regressions before they reach prod. |
| Production (`deploy/cloudrun.yaml`) | `true` | `deploy/cloudrun.yaml` env block | The product's headline claim — "24/7 autonomous platform" — is now literally true only because this is set. |

Rollback: set the flag to `false` in any environment and the loop simply
never starts — no other behavior changes, no data consequences.

## 3. Worker supervision (heartbeats)

Two background daemon threads run continuously once started:
`MonitorAgent` (gated, see above) and `IngestionWorker` (always started,
drains `POST /api/v1/ingest/upload` jobs).

Before E7, a dead thread inside either loop was **invisible** — nothing
detected it; an operator would only notice symptomatically (no new
incidents, or documents stuck `queued` forever), i.e. "discovered," not
"detected."

E7 adds a shared, thread-safe `HeartbeatTracker`
(`aeam/monitoring/metrics.py`). Each loop iteration — **unconditionally,
whether or not that iteration's own work succeeded** — calls
`heartbeat_tracker.record("monitor")` / `record("ingestion")` before
doing any work. This proves the *thread* is alive; cycle-level failures
are still caught, logged, and metered exactly as before — they are a
separate, already-covered concern.

`GET /health` reports, per worker:

| `checks.<worker>` value | Meaning |
|---|---|
| `disabled (ENABLE_MONITOR_AGENT=false)` | Never constructed (MonitorAgent only). |
| `not started` | Never constructed (should not occur for IngestionWorker — always started). |
| `starting (no heartbeat yet)` | Thread constructed/started but has not completed its first loop iteration. |
| `ok (last heartbeat Ns ago)` | Alive; `N <= HEARTBEAT_STALE_SECONDS`. |
| `stale (last heartbeat Ns ago)` | `N > HEARTBEAT_STALE_SECONDS` — the thread has stopped updating its heartbeat. Flips overall `status` to `"degraded"` and the HTTP status to `503`. |

The same fact is also published as a Prometheus gauge
(`worker_heartbeat_timestamp_seconds{worker="monitor"|"ingestion"}`) for
scrape-based alerting, and surfaced in the console footer (StatusBar)
as "Monitor" / "Ingestion" chips reading the same `/health` payload.

**Operational response to a "stale" worker:** the process is still
running (the ASGI server itself is healthy — only the specific daemon
thread died or wedged), so the standard remedy is a full instance
restart (Cloud Run's liveness probe already restarts on `/health`
failure) — there is no in-process thread-restart mechanism, matching
the existing "log and keep polling" resilience contract for *cycle*
failures but not thread death itself.

## 4. BM25 lexical index freshness (RAG-6)

`HybridRetrievalPipeline` fuses dense (Qdrant) and lexical (in-memory
`BM25Index`) retrieval. Before E7, the BM25 index was built once at
startup (`BM25Index.from_qdrant`) and never refreshed — a document
ingested at runtime was immediately dense-retrievable (Qdrant is written
directly) but **not** lexically retrievable until the next restart,
silently skewing hybrid fusion toward whatever existed at boot.

E7 adds `BM25Index.refresh_from_qdrant(qdrant_client, collection)` — an
in-place rebuild using the exact same scroll path `from_qdrant` uses at
startup (`BM25Index._scroll_qdrant_documents`, extracted so both share
one implementation). `DocumentIngestJobProcessor` calls it immediately
after a document's chunks are successfully indexed into Qdrant. Because
the rebuild mutates the *existing* `BM25Index` instance in place, every
holder of a reference to it (the `HybridRetrievalPipeline` wrapping it)
observes the refresh with no additional wiring.

A refresh failure is logged and swallowed — it never fails the
otherwise-successful ingestion job. The index simply stays at its
previous (still-usable) state, and its staleness is disclosed honestly
rather than silently.

`GET /health`'s `checks.bm25_index` field:

| Value | Meaning |
|---|---|
| `disabled (RAG_HYBRID_ENABLED=false or init failed)` | No BM25 index exists this run. |
| `unbuilt` | Index constructed but `build()` never completed (should not occur in practice — `from_qdrant` always calls `build()`). |
| `ok (built Ns ago, M docs)` | `N <= BM25_STALE_SECONDS`. |
| `stale (built Ns ago, M docs)` | `N > BM25_STALE_SECONDS` — informational only; does **not** flip overall health, since a stale lexical index degrades retrieval *quality*, not platform *availability*. |

## 5. Multi-instance posture (dedup / idempotency)

AEAM's dedup (`EventDeduplicator`) and action idempotency
(`IdempotencyManager`) are both Redis-backed (`SET ... NX`/`SETEX`
against the shared Redis instance) — never in-process memory. Running N
instances of the app (e.g. Cloud Run `maxScale: 5`) is therefore safe
for both concerns without any additional coordination: two instances
racing to publish "the same" event, or execute "the same" action, share
one Redis-backed lock domain and only one wins. This was already true
before E7; E7's contribution is documenting it explicitly (per the
ROADMAP's "multi-instance posture ... re-verified ... documented" line)
and adding a regression test proving two independently-constructed
`EventDeduplicator`/`IdempotencyManager` instances sharing one Redis
client behave as a single shared domain.

`MonitorAgent` and `IngestionWorker` are each **single-writer per
instance** — every app instance runs its own copy of both loops. This is
intentional and safe: `MonitorAgent`'s detected events are deduplicated
downstream (Redis-shared), and `IngestionWorker`'s job claims use
`next_queued()` (a DB-level claim), so N instances polling the same job
queue simply race for jobs, never double-process one.

## 6. Debug retrieval surface stays off in production

Unrelated to autonomy directly, but part of this phase's acceptance
criteria: `GET /api/v1/debug/retrieval/` returns `404` (not `403` —
existence itself is not disclosed) whenever `ENVIRONMENT == "production"`
(`aeam/api/retrieval_debug.py`). This predates E7 and is verified by a
regression test in this phase's test suite so it never silently
regresses.
