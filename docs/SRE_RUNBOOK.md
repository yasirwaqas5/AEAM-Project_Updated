# AEAM SRE Runbook — operating the platform itself

**Phase E11 (OBS-6).** Every other runbook in this repository tells AEAM how
to investigate *your* systems. This one tells *you* how to operate AEAM.

Each section follows the same shape: **symptom → the metric that proves it →
what to do**. Every metric named here is published by
`aeam/monitoring/metrics.py` at `GET /metrics` and scraped by
`deploy/prometheus.yml`; every alert named here is defined in
`deploy/alerts.yml` and catalogued with its declared semantics in
[ALERT_CATALOG.md](ALERT_CATALOG.md).

## Before you start

| What | Where |
|---|---|
| Metrics | `GET /metrics` (Prometheus exposition) |
| Dependency health | `GET /health` — database, redis, queue, monitor_agent, ingestion_worker, bm25_index |
| Platform status | `GET /api/v1/system/status` |
| Self-observability summary | `GET /api/v1/observability/` — hit rates, duration, cost, AI health |
| Audit trail | `GET /api/v1/audit/entries` (auditor role) |
| Traces | your OTLP backend, filtered on `aeam.incident_id` |

`/health` and `/` are public. Everything else needs a bearer token; in a
non-development environment `/metrics` does too, which is why the scrape
config carries a `credentials_file`.

---

## AEAM is down

**Alert:** `AEAMDown` · **Severity:** critical

**Symptom.** Prometheus cannot scrape `/metrics` for 2 minutes.

**Confirm.**
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<host>/health
```

**Diagnose, in order.**
1. `200` from `/health` but the alert is firing → the scrape is failing, not
   the platform. Almost always the bearer token: `SecurityMiddleware` returns
   401 for `/metrics` outside development. Check the `credentials_file` the
   scrape config points at.
2. Non-200 or connection refused → the process is down or unreachable. Check
   the container/instance status and the application log for a startup abort.
3. A **deliberate** startup abort is the most likely cause of a clean
   non-start: AEAM fails closed on purpose when
   - no JWT public key is configured outside development (Phase E3, SEC-4), or
   - `LLM_PROVIDER` names a provider that is not implemented (Phase E8).

   Both write an explicit message naming the setting. Fix the configuration;
   do not work around the abort.

---

## MonitorAgent heartbeat is stale

**Alert:** `AEAMMonitorAgentHeartbeatStale` · **Severity:** critical

**Symptom.** `time() - worker_heartbeat_timestamp_seconds{worker="monitor"} > 180`.

**What it means.** Autonomous detection has stopped. No KPI anomaly will be
detected until the thread recovers. Events can still enter through
`POST /api/v1/trigger` and `run_simulation.py`, so the platform is not fully
blind — but its headline capability is not running.

**Confirm.** `GET /health` → `checks.monitor_agent` reports the same fact in
words (`"stale (last heartbeat 130s ago)"`). The console StatusBar shows it
as a degraded chip.

**Diagnose.**
- `"disabled (ENABLE_MONITOR_AGENT=false)"` — not a fault. The monitor is
  switched off by configuration. If that is wrong for this environment, set
  `ENABLE_MONITOR_AGENT=true`. Phase E7 made this flag authoritative in both
  directions: there is no environment backdoor that turns it on or off behind
  your back.
- `"stale (...)"` — the thread is wedged or dead. Check the application log
  for the last `MonitorAgent` cycle and any traceback. Restart the instance;
  the monitor thread is started at lifespan boot and there is no
  restart-in-place path.
- Cycle-level errors are logged and metered separately. A heartbeat proves
  the *thread* is alive, not that its last cycle succeeded — that is
  deliberate.

---

## IngestionWorker heartbeat is stale

**Alert:** `AEAMIngestionWorkerHeartbeatStale` · **Severity:** warning

**Symptom.** No ingestion heartbeat for 5 minutes.

**What it means.** Queued ingestion jobs are not being claimed. Uploads stay
`QUEUED` and never become retrievable. **Existing retrieval is unaffected** —
this degrades the corpus's freshness, not the platform's ability to
investigate.

**Confirm.**
```bash
curl -sS '<host>/api/v1/ingest/jobs?status=queued' -H "Authorization: Bearer $TOKEN"
```
A growing queued list alongside a stale heartbeat confirms it.

**Act.** Restart the instance. On recovery the worker claims the backlog in
order; no upload is lost, because the blob and the job row are both durable.

---

## Action failure rate is high

**Alert:** `AEAMActionFailureRateHigh` · **Severity:** warning

**Symptom.** Over 25% of `ActionAgent` executions failed in 10 minutes.

**What it means.** Investigations are still completing and recording
evidence. It is the **output** side that is degraded — Slack, Jira, email,
webhooks.

**Diagnose.** Break the rate down by integration:
```promql
sum by (action_type) (rate(action_failure_total[10m]))
```
Then check that integration's credentials and the circuit breaker state in
the application log. A missing-credentials failure for email is expected in
most deployments and is reported with a structured reason rather than a
crash — do not chase it unless email is actually configured.

---

## No actions executed

**Alert:** `AEAMNoActionsExecuted` · **Severity:** warning

**Symptom.** `increase(action_executions_total{outcome="executed"}[1h]) == 0`.

**What it means.** Ambiguous by design, so check the other half:
- `incidents_total` also flat → the platform is genuinely idle. Nothing to do.
- `incidents_total` climbing → investigations are completing but every action
  is being withheld or skipped.

**Diagnose the withheld case.** Phase E9's approval gate withholds gated
steps until an authorised approval arrives:
```bash
curl -sS '<host>/api/v1/review/queue' -H "Authorization: Bearer $TOKEN"
```
A deep queue means governance is working and nobody is reviewing. That is an
organisational problem, not a platform fault — but it is exactly what this
alert is for.

---

## Investigations are slow

**Alert:** `AEAMInvestigationDurationHigh` · **Severity:** warning

**Symptom.** Rolling mean investigation duration above 120s.

**Diagnose, in order.**
1. **LLM latency** — `rate(llm_call_duration_seconds_sum[15m]) /
   rate(llm_call_duration_seconds_count[15m])`. Provider slowness is the
   usual cause. `LLM_TIMEOUT_SECONDS` bounds each call.
2. **Per-agent timing** — `agent_execution_time_seconds` by `agent` label
   isolates rag / forecast / report / action.
3. **Traces** — with tracing enabled, one investigation is one trace; the
   slow stage is visible directly rather than inferred. Filter your OTLP
   backend on `aeam.incident_id`.
4. **Per-incident detail** — `audit_summary.investigation_duration_seconds`
   is persisted on every incident since Phase E11, and rendered on the
   Analytics page with min/median/max.

---

## Active incidents stuck

**Alert:** `AEAMActiveIncidentsStuck` · **Severity:** critical

**Symptom.** `active_incidents` non-zero and unchanged for 30 minutes.

**What it means.** An investigation started and never reached
`finalize_incident()`, which is what decrements the gauge. Something in the
investigation path is wedged — most often an external call without an
effective timeout.

**Act.** Find the incident in the trace backend (its root span will have no
end), or in the log by the incident id every investigation-path line carries.
Restarting clears the gauge but loses the in-flight investigation; the event
can be re-triggered.

---

## LLM failure rate is high

**Alert:** `AEAMLLMFailureRateHigh` · **Severity:** warning

**Symptom.** Over 20% of LLM calls failed in 10 minutes.

**What it means.** AEAM degrades honestly here: investigations continue on
the deterministic path and record the failure rather than fabricating a
result. Grounded root-cause quality drops; nothing becomes untrue.

**Diagnose.** `llm_calls_total` by `provider` and `status`. A `provider="mock"`
label means `USE_MOCK_LLM` is on or `LLM_ENABLED` is off — check that is
intended for this environment. Otherwise: provider status page, credentials,
and `LLM_TIMEOUT_SECONDS`.

---

## LLM spend spike

**Alert:** `AEAMLLMSpendSpike` · **Severity:** warning

**Symptom.** Estimated spend above $5/hour.

**Read the semantics before acting.** This figure is **operator-priced**:
provider-reported token counts multiplied by
`LLM_COST_PER_1K_PROMPT_TOKENS_USD` / `LLM_COST_PER_1K_COMPLETION_TOKENS_USD`.
It is informational, never an invoice. If those rates are left at their
honest `0.0` default, **this alert cannot fire and its silence means
nothing** — configure the rates before relying on it.

**Diagnose.** The Analytics cost surface breaks spend down per incident and
per window. A spike is usually retrieval pool growth (more context per call)
rather than more calls; `retrieval_chunks_total` distinguishes the two.

---

## Answering an audit question

Compliance can self-serve since Phase E11. The trail is append-only and the
query surface is read-only — there is no endpoint that can modify it.

```bash
# What did one principal do, in one window?
curl -sS '<host>/api/v1/audit/entries?principal=alice@example.com\
&since=2026-07-01T00:00:00Z&until=2026-07-31T23:59:59Z' \
  -H "Authorization: Bearer $TOKEN"

# Who has been active at all?
curl -sS '<host>/api/v1/audit/principals?since=2026-07-01T00:00:00Z' \
  -H "Authorization: Bearer $TOKEN"
```

`X-Total-Count` carries the pre-paging total. The `auditor` role holds the
`logs:view` grant these endpoints require.

Knowledge-governance actions (policy retirement, memory correction, semantic
typing) write to the same trail with actions `policy_status_changed`,
`memory_expunged`, `memory_corrected`, and
`document_semantic_type_declared` — see
[KNOWLEDGE_GOVERNANCE.md](KNOWLEDGE_GOVERNANCE.md).

---

## Enabling tracing

Off by default. Both settings are required:

```bash
OTEL_TRACING_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=aeam
```

Requires the optional `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` packages. If they are absent, or the
endpoint is empty, tracing stays **off with a loud warning** rather than
silently dropping spans or failing startup — a telemetry backend must never
be able to stop the platform.

One investigation produces one trace: `investigation` (root) → `decision` →
`evidence.memory` / `evidence.policy` / `evidence.cross_dataset` /
`evidence.adaptive_detection` / `evidence.rag` → `planning` → `action`. Every
span carries `aeam.incident_id`, the same key already on the log lines, so
logs and traces join without a second correlation scheme. The trace id is
also persisted into the incident's `audit_summary.trace_id`.

**Metrics remain the single metrics pipeline (OBS-1).** Spans are a
complementary signal, not a replacement — do not build dashboards on span
counts when a metric already answers the question.
