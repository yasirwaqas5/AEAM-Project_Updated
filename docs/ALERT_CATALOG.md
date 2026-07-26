# AEAM Alert Catalog

**Phase E11 (OBS-1, OBS-2, OBS-6).** Every alert AEAM ships, with its
**declared semantics** — what the number actually means, what it does *not*
mean, and what silence from it proves (often: nothing).

The rules themselves live in [`deploy/alerts.yml`](../deploy/alerts.yml) and
are loaded by [`deploy/prometheus.yml`](../deploy/prometheus.yml). Response
procedures live in [SRE_RUNBOOK.md](SRE_RUNBOOK.md); this document is the
reference for what each alert *is*.

## Constitutional position

- **OBS-1 — one metrics pipeline.** Every expression below evaluates over
  series already published by `aeam/monitoring/metrics.py`. No alert
  introduced a metric, and no deployment artifact configures a pushgateway,
  statsd, graphite, or a second remote-write target. A test asserts both
  facts (`test_every_alert_expression_references_a_metric_aeam_actually_publishes`,
  `test_no_deployment_artifact_defines_a_second_metrics_store`).
- **OBS-2 — declared semantics.** Each alert carries `summary`,
  `description`, and `runbook` annotations. An alert with no declared
  semantics is unactionable at 3am; a test rejects one that lacks them.
- **Severity vocabulary.** `critical` = the platform is not performing its
  core function; page. `warning` = degraded or trending badly; investigate
  within the hour. There is no third level, deliberately.

## Reading the "silence proves" column

An alert that *cannot* fire is worse than no alert, because it looks like
coverage. Where an alert depends on optional configuration, that is stated
explicitly rather than left for an operator to discover during an incident.

---

## `aeam.availability`

### AEAMDown

| | |
|---|---|
| **Severity** | critical |
| **Expression** | `up{job="aeam"} == 0` |
| **For** | 2m |
| **Means** | Prometheus has not successfully scraped `/metrics` for 2 minutes. |
| **Does NOT mean** | That the process is necessarily dead. A rejected scrape credential produces the same signal — `/metrics` is not a public path outside development. |
| **Silence proves** | That the scrape succeeded. It does not prove investigations are completing; see `AEAMActiveIncidentsStuck`. |
| **Runbook** | [AEAM is down](SRE_RUNBOOK.md#aeam-is-down) |

---

## `aeam.autonomy`

Both rules read `worker_heartbeat_timestamp_seconds`, the Prometheus view of
Phase E7's `HeartbeatTracker`. `time() - <gauge>` is the heartbeat's age. A
heartbeat proves the **thread is alive**, not that its last cycle succeeded —
cycle failures are logged and metered separately, and that separation is
deliberate.

### AEAMMonitorAgentHeartbeatStale

| | |
|---|---|
| **Severity** | critical |
| **Expression** | `time() - worker_heartbeat_timestamp_seconds{worker="monitor"} > 180` |
| **For** | 1m |
| **Means** | Autonomous detection has stopped. No KPI anomaly will be detected until the thread recovers. |
| **Does NOT mean** | The platform is blind. Events still enter via `POST /api/v1/trigger` and `run_simulation.py`. |
| **Silence proves** | The monitor thread is alive — **provided it is enabled**. When `ENABLE_MONITOR_AGENT=false` the gauge is never set, so this alert cannot fire. `GET /health` reports `"disabled (ENABLE_MONITOR_AGENT=false)"` in that case; check there before trusting silence. |
| **Threshold rationale** | 180s is comfortably longer than a normal poll cycle, so a slow cycle never pages. |
| **Runbook** | [MonitorAgent heartbeat is stale](SRE_RUNBOOK.md#monitoragent-heartbeat-is-stale) |

### AEAMIngestionWorkerHeartbeatStale

| | |
|---|---|
| **Severity** | warning |
| **Expression** | `time() - worker_heartbeat_timestamp_seconds{worker="ingestion"} > 300` |
| **For** | 2m |
| **Means** | Queued ingestion jobs are not being claimed. Uploads stay `QUEUED` and never become retrievable. |
| **Does NOT mean** | Retrieval is broken. The existing corpus is unaffected; only its *freshness* degrades. |
| **Silence proves** | The ingestion thread is alive. It does not prove individual jobs are succeeding — a job that fails is recorded with its error and does not stop the heartbeat. |
| **Severity rationale** | `warning`, not `critical`: the platform's core function (investigation) continues throughout. |
| **Runbook** | [IngestionWorker heartbeat is stale](SRE_RUNBOOK.md#ingestionworker-heartbeat-is-stale) |

---

## `aeam.actions`

### AEAMActionFailureRateHigh

| | |
|---|---|
| **Severity** | warning |
| **Expression** | `sum(rate(action_failure_total[10m])) / clamp_min(sum(rate(action_success_total[10m])) + sum(rate(action_failure_total[10m])), 0.001) > 0.25` |
| **For** | 10m |
| **Means** | Over a quarter of `ActionAgent` executions failed. The **output** side is degraded — Slack, Jira, email, webhooks. |
| **Does NOT mean** | Investigations are failing. They complete and record evidence regardless; only the acting-on-it step is affected. |
| **Silence proves** | Either actions are mostly succeeding, or **no actions ran at all** — the `clamp_min` guard keeps the expression defined at zero traffic, which means it evaluates to 0 and stays quiet. Pair with `AEAMNoActionsExecuted`. |
| **Known-benign cause** | A deployment with no email credentials reports a structured failure per incident. Expected; do not chase unless email is configured. |
| **Runbook** | [Action failure rate is high](SRE_RUNBOOK.md#action-failure-rate-is-high) |

### AEAMNoActionsExecuted

| | |
|---|---|
| **Severity** | warning |
| **Expression** | `sum(increase(action_executions_total{outcome="executed"}[1h])) == 0` |
| **For** | 1h |
| **Means** | Deliberately ambiguous — it is a *prompt to check*, not a verdict. |
| **Interpretation** | Cross-reference `incidents_total`. Flat too → genuinely idle, nothing to do. Climbing → investigations complete but every action is withheld or skipped, most often a Phase E9 approval queue nobody is reviewing. |
| **Silence proves** | At least one action succeeded in the last hour. |
| **Runbook** | [No actions executed](SRE_RUNBOOK.md#no-actions-executed) |

---

## `aeam.investigations`

### AEAMInvestigationDurationHigh

| | |
|---|---|
| **Severity** | warning |
| **Expression** | `rate(investigation_duration_seconds_sum[15m]) / clamp_min(rate(investigation_duration_seconds_count[15m]), 0.001) > 120` |
| **For** | 15m |
| **Means** | Rolling mean end-to-end investigation wall-clock time exceeds 2 minutes. |
| **Relationship to the persisted value** | This is the *same measurement* the Orchestrator persists per incident into `audit_summary.investigation_duration_seconds` — one measurement, two consumers. The Analytics page renders the persisted per-incident view (min / median / max); this alerts on the aggregate. They can never disagree. |
| **Silence proves** | The mean is under budget. It says nothing about outliers — a single 20-minute investigation is invisible here but plainly visible in the Analytics `max`. |
| **Runbook** | [Investigations are slow](SRE_RUNBOOK.md#investigations-are-slow) |

### AEAMActiveIncidentsStuck

| | |
|---|---|
| **Severity** | critical |
| **Expression** | `active_incidents > 0 and changes(active_incidents[30m]) == 0` |
| **For** | 30m |
| **Means** | An investigation started and never reached `finalize_incident()`, which is what decrements the gauge. Something in the investigation path is wedged. |
| **Does NOT mean** | High load. A busy platform changes this gauge constantly; it is the *absence of change* that is the signal. |
| **Silence proves** | Either the gauge is moving (healthy) or it is at zero (idle). Both are fine. |
| **Runbook** | [Active incidents stuck](SRE_RUNBOOK.md#active-incidents-stuck) |

---

## `aeam.ai`

### AEAMLLMFailureRateHigh

| | |
|---|---|
| **Severity** | warning |
| **Expression** | `sum(rate(llm_calls_total{status="failure"}[10m])) / clamp_min(sum(rate(llm_calls_total[10m])), 0.001) > 0.20` |
| **For** | 10m |
| **Means** | Over a fifth of LLM calls failed. Grounded root-cause quality drops. |
| **Does NOT mean** | Anything AEAM reports has become untrue. The platform degrades honestly: investigations continue on the deterministic path and record the failure rather than fabricating a result. |
| **Silence proves** | Calls are mostly succeeding — **or the platform is on the mock path**. `llm_calls_total{provider="mock"}` distinguishes them; check `USE_MOCK_LLM` / `LLM_ENABLED` before trusting silence in a supposedly-live environment. |
| **Runbook** | [LLM failure rate is high](SRE_RUNBOOK.md#llm-failure-rate-is-high) |

### AEAMLLMSpendSpike

| | |
|---|---|
| **Severity** | warning |
| **Expression** | `sum(rate(llm_cost_usd_total[1h])) * 3600 > 5` |
| **For** | 30m |
| **Means** | Estimated hourly LLM spend above $5. |
| **Cost basis** | Provider-reported token counts × the operator-configured `LLM_COST_PER_1K_PROMPT_TOKENS_USD` / `LLM_COST_PER_1K_COMPLETION_TOKENS_USD` rates. **Informational, never an invoiced total.** |
| **Silence proves** | **Possibly nothing.** Both rate settings default to an honest `0.0`, which pins `llm_cost_usd_total` at zero and makes this alert unfireable. Configure the rates before relying on it. |
| **Threshold** | `$5/hour` is a placeholder appropriate to a single-instance deployment. Tune it to your budget; it is not derived from anything. |
| **Runbook** | [LLM spend spike](SRE_RUNBOOK.md#llm-spend-spike) |

---

## Deliberately absent alerts

Stating what is *not* alerted on is part of the catalog's job — an operator
should never assume coverage that does not exist.

| Not alerted | Why |
|---|---|
| BM25 index staleness | `GET /health` discloses it (`checks.bm25_index`) and Phase E7 refreshes it on ingestion completion. A stale lexical index degrades hybrid retrieval quality slightly; it is not an operational incident. |
| Qdrant / LLM dependency health | No proxied endpoint exposes them today. The console shows an honest `n/a` chip rather than fabricating green. Alerting on a signal that does not exist would be worse. |
| Per-incident cost | A single expensive incident is a fact, not a fault. The Analytics cost surface is the right instrument; a budget-enforcement alert needs hard limits, which are future work. |
| Retrieval quality regression | Caught in gating CI by the Phase E12 golden-set harness, not at runtime. Corpus drift is a build-time concern. |
