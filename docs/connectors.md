# Enterprise Connectors — operator guide

**Phase F7 — Enterprise Connector Framework & Data-Source Connectors.**

Before this phase content entered AEAM two ways: manual upload, and one Google
Sheets KPI connector. Enterprise deployments need knowledge and metrics from the
systems the organization already runs. F7 generalizes the existing connector
pattern into a framework and implements eight connectors — **without a second
ingestion path**.

---

## The architecture in one sentence

Every connector reaches the platform through a composition point that already
existed: document connectors through `IngestionSubmitter` (the upload path), and
metric connectors through `CompositeKPISource` (the Sheets path).

```
                            ┌─────────────────────────────────┐
  SharePoint ─┐             │      IngestionSubmitter         │
  Confluence ─┤  documents  │  (aeam/ingestion/submission.py) │
  GitHub     ─┼────────────▶│  validate → BlobStore.put →     │
  Drive      ─┘             │  dedup → Document + INGEST job  │
                            └────────────────┬────────────────┘
                                             │  ← the SAME path
  POST /api/v1/ingest/upload ────────────────┘     an upload takes
                                             │
                            ┌────────────────▼────────────────┐
                            │  IngestionWorker (unchanged)    │
                            │  DocumentIngestJobProcessor     │
                            │  chunk → embed → index (Qdrant) │
                            └─────────────────────────────────┘

  SAP        ─┐             ┌─────────────────────────────────┐
  Salesforce ─┤   metrics   │      CompositeKPISource         │
  Snowflake  ─┼────────────▶│  fetch_rows(selector) members   │──▶ MonitorAgent
  BigQuery   ─┘             │  + Google Sheets + datasets     │    (unchanged)
                            └─────────────────────────────────┘
```

There is **no** second chunker, embedder, indexer, retriever, detector, job
type, or KPI path. A SharePoint page and an uploaded PDF produce the same
`documents` row, the same active `versions` row, the same `JobType.INGEST` job,
and the same chunks in the same collection.

---

## The connector contract

One ABC, `aeam/connectors/base.py::EnterpriseConnector`. Every connector
implements the whole of it and nothing outside it:

| Method | Purpose |
|---|---|
| `validate_config()` | `(ok, reason)` — configuration is checkable before anything runs |
| `authenticate()` | resolve credentials via `SecretManager`, verify reachability |
| `list_artifacts(since)` | upstream artifacts, cheap; supports server-side filtering |
| `fetch_artifact(ref)` | download one artifact's bytes — the expensive call |
| `fetch_rows(selector)` | the **existing** `KPIRowSource` protocol |
| `describe()` / `health()` | credential-free self-description |
| `close()` | release the transport and forget resolved credentials |

A connector declares a **capability** (`documents` or `metrics`), and the sync
engine dispatches on that declaration rather than on the class. Both capability
methods are concrete on the base with honest defaults — a metrics connector's
`list_artifacts()` returns `[]` because it genuinely has no documents — which
keeps the contract uniform so one shared test suite can call every method on
every connector.

Connector-specific behaviour lives **only** inside connector implementations,
and mostly as declarative data: a `ListingFieldMap` naming the upstream's fields
and a client factory. A ninth document connector is a ~40-line module.

---

## The eight connectors

| Connector | Capability | Change signal | Required config |
|---|---|---|---|
| SharePoint | documents | `eTag` (content-derived) | `site_url`, `drive_id` |
| Confluence | documents | monotonic page version | `base_url`, `space_key` |
| GitHub | documents | git blob `sha` (content-addressed) | `repository`, `path` |
| Google Workspace (Drive) | documents | Drive `version` | `folder_id` |
| SAP | metrics | n/a (queried per cycle) | `host`, `client`, `query` |
| Salesforce | metrics | n/a | `instance_url`, `soql` |
| Snowflake | metrics | n/a | `account`, `warehouse`, `database`, `query` |
| BigQuery | metrics | n/a | `project_id`, `dataset`, `query` |

The Drive **document** connector is deliberately separate from the existing
Google Sheets **KPI** connector, which this phase leaves untouched. Two
different jobs against one vendor; merging them would give one connector two
capabilities and break the uniform contract.

---

## Credentials (SEC-5)

A connector never receives, stores, logs, or returns a credential value. It
receives a `SecretManager` and a `secret_ref` — a **name** — and resolves the
value at the moment it authenticates.

* `sources.secret_ref` holds the name; the value lives only in the environment
  or a secret manager.
* `describe()`/`health()` expose configuration **keys** and the secret **name**,
  never a value. The name is not sensitive, and hiding it would make a
  misconfiguration undiagnosable.
* `sanitize_error()` scrubs every resolved secret out of any message before it
  reaches a log, a persisted `connector_sync_runs.error`, an API response, or
  the health surface — because upstream systems routinely echo a token back in
  an error body, and by the time a message reaches a log it is too late.
* `close()` clears resolved credentials from memory.

Asserted per connector: no credential in `describe()`, none in `health()`, none
in any API response, and an echoed token is redacted while the surrounding
diagnostic context survives.

---

## Incremental sync

A run does the least work the artifact's state allows. Three exits, cheapest
first:

1. **upstream says unchanged** — the recorded change signature matches, so
   nothing is downloaded. Where server-side `since` filtering exists (Graph,
   Confluence, Drive) the artifact is not even *listed*.
2. **bytes are unchanged** — upstream exposed no signature (a plain file share,
   a native Doc), so the bytes were fetched and hashed, and the hash matches.
   No submission, no re-embedding.
3. **changed or new** — submitted through the existing pipeline.

### Idempotency, four layers deep

A repeated sync with no upstream change creates no duplicate document,
embedding, metadata row, or job. Any one of these would be sufficient:

| Layer | Mechanism |
|---|---|
| change signature | short-circuits before `fetch_artifact` |
| content hash | short-circuits before submission |
| existing dedup | identical bytes reuse the blob, the in-flight job, and the `Document` |
| processor | no-ops on an already-`indexed` document |

The connector needs no idempotency mechanism of its own, because submitting the
same bytes twice is already a no-op four times over.

**The cursor advances only on a successful (or partial) run.** A failed run must
not advance it, or the artifacts it never fetched would be skipped forever.

---

## Failure isolation

A connector failure never blocks another connector, ingestion, KPI collection,
retrieval, or startup. Each guarantee has a mechanism:

| Guarantee | Mechanism |
|---|---|
| one connector cannot block another | `sync_all()` wraps each connector's run |
| one artifact cannot abandon a run | `sync_source()` wraps each artifact; the run reports `partial` |
| a connector cannot block ingestion | sync runs live in their **own** ledger, not in `ingestion_jobs` — so connector work never occupies the `IngestionWorker` thread |
| a connector cannot block KPI collection | `fetch_rows()` **never raises**; it returns `[]` and records the reason, which is the no-op `MonitorAgent` already handles |
| a connector cannot block startup | composition failures are logged and skipped |
| nothing escapes | the isolation handler resolves its sanitiser defensively, so a connector broken enough to lack `sanitize` cannot raise *inside* the handler |

An artifact the platform cannot ingest (an `.exe` in a document library) is a
**skipped** artifact with a stated reason, not a run failure.

A run's status is `succeeded`, `partial`, `failed`, or `running`. `partial` and
`running` exist because collapsing them into success or failure would misstate
what the connector actually did.

---

## Connector health (SEC-8)

`GET /api/v1/connectors/health` reports twelve fields per connector: `enabled`,
`configured`, `authenticated`, `last_successful_sync`, `last_failed_sync`,
`sync_status`, `stale`, `processed_count`, `skipped_count`, `changed_count`,
`sync_duration_seconds`, `error_reason` — plus the catalog of all eight
connector types, including ones nobody has configured.

**Unknown is never reported as healthy.**

* a connector that has never synced reports `sync_status: "never_synced"` and
  `stale: null` **with a reason** — not `false`. "We cannot tell whether this is
  stale" and "this is fresh" are different answers.
* `authenticated` is `false` until a call actually succeeded — never
  optimistically true.
* `unknown` is its own summary bucket. A connector that is enabled and
  configured but has never synced is genuinely neither healthy nor unhealthy.
* "last successful sync" and "last failed sync" are separate facts, because a
  connector that succeeded an hour ago and failed a minute ago is in a different
  state from one that has only ever failed.

Health integrates into the **existing** observability layer: gauges and counters
registered in `aeam/monitoring/metrics.py` (`connector_up`,
`connector_last_sync_timestamp_seconds`, `connector_sync_artifacts_total`,
`connector_sync_duration_seconds`). No second monitoring subsystem, no polling
loop — the report is computed on read.

`connector_up` publishes `0` rather than nothing for an enabled-but-broken
connector: a missing series looks the same as a healthy one to most alerting
rules.

---

## Provenance

Every ingested artifact keeps a `connector_artifacts` row: connector, upstream
id, upstream's own type (verbatim, never mapped), source URL, sync timestamp,
original source timestamp, semantic doc type, upstream version, the local
`Document`/`Dataset` it became, the ingestion job that made it, and the
skip/ingest counters.

**A field upstream does not expose is `null`, never filled in.** A fabricated
timestamp would make incremental sync silently wrong; a fabricated URL would
send an operator to a page that does not exist. GitHub's contents API exposes no
per-file timestamp, so GitHub provenance reports none rather than borrowing a
commit date that changes for untouched files.

---

## API

| Endpoint | Purpose | RBAC |
|---|---|---|
| `GET /api/v1/connectors/health` | catalog + per-connector health | `documents:ingest` |
| `GET /api/v1/connectors/` | same payload | `documents:ingest` |
| `GET /api/v1/connectors/{id}` | one connector's health | `documents:ingest` |
| `GET /api/v1/connectors/{id}/artifacts` | provenance, bounded | `documents:ingest` |
| `GET /api/v1/connectors/{id}/runs` | sync history, bounded | `documents:ingest` |
| `POST /api/v1/connectors/sync` | sync every enabled connector | `admin:config` |
| `POST /api/v1/connectors/sync/{id}` | sync one connector | `admin:config` |

The per-connector trigger is `POST /sync/{id}` and **not** `POST /{id}/sync`.
The RBAC map matches on path alone, not method, so a write nested under a read
prefix would be graded as a read — which is how an analyst ends up able to fire
a sync with organizational credentials. A test asserts no non-GET route exists
outside the `/sync` prefix.

A connector fault returns **200 with a failed outcome**, not a 5xx: an isolated
connector failure is a recorded state, and a 5xx would make it look like a
platform failure.

```bash
curl -X POST http://localhost:8080/api/v1/connectors/sync/<source_id> -H 'Content-Type: application/json' -d '{}'
```

Sync is **operator-triggered only**. There is no timer and no autonomous poll,
matching the repo's existing posture (the APScheduler stub was removed in E1) —
and, more importantly, keeping connector work off the ingestion worker's thread.

---

## Testing without a tenant

Gating CI never calls SharePoint, Confluence, GitHub, Drive, SAP, Salesforce,
Snowflake, or BigQuery. It cannot: those calls need a tenant, a licence, and a
credential.

Every connector takes its transport by **injection**. Tests pass a deterministic
in-repo client from `aeam/connectors/enterprise/mock.py`, which models each
vendor's **real** field naming as a `MockDialect` declared independently of the
connector's `ListingFieldMap`. That independence is what makes the suite a real
test: a connector whose field map disagrees with its vendor's naming translates
nothing and fails.

Structurally enforced too: the real REST client cannot be constructed without a
resolved credential, so with no credential and no injected client there is
nothing that could issue a request.

`CONNECTOR_MOCK_MODE=true` uses the same clients in a running deployment, so an
operator can watch a full sync — listing, change detection, ingestion,
retrieval — before any credential exists. It is honest about itself: health
reports `mock_mode: true` and the console labels it.

---

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `CONNECTORS_ENABLED` | `false` | Master switch — off disables every connector |
| `CONNECTOR_MOCK_MODE` | `false` | Use deterministic in-repo clients |
| `CONNECTOR_<KIND>_ENABLED` | `false` | Per-connector gate (eight of them) |
| `CONNECTOR_SYNC_MAX_ARTIFACTS` | `500` | Per-run artifact cap (E6) |
| `CONNECTOR_STALE_AFTER_SECONDS` | `86400` | Staleness threshold |

Registering a connector is a `sources` row: `kind` (one of the eight), `config`
(non-secret parameters), and `secret_ref` (the credential's **name**).

---

## Rollback

`CONNECTORS_ENABLED=false` and restart. Zero connectors enabled means: no
connector is built, the sync engine finds no connector-kind sources, no metrics
member joins `CompositeKPISource`, and health honestly reports an empty roster.
The framework ABC is inert.

The two tables are additive and stay empty. Manual upload and the existing
Google Sheets KPI connector behave exactly as they did before this phase.

---

## Standing limits (stated, not worked around)

* **Sync is operator-triggered.** No schedule exists, so a connector is only as
  current as its last manual sync. Autonomous polling would need the E7 worker
  posture and its own isolation story.
* **Real vendor SDKs are not platform dependencies.** `pyrfc`,
  `simple-salesforce`, `snowflake-connector-python`, and
  `google-cloud-bigquery` are not installed; a metrics connector without its SDK
  reports `authenticated: false` naming the missing module rather than degrading
  to a silent zero rows. Install the SDK and inject a client to make it live.
* **The four REST document clients are untested by gating CI** by design —
  they are the one part of each connector that only a real tenant exercises. The
  translation logic above them is fully covered.
* **A metrics connector's rows are re-queried every monitoring cycle.** There is
  no result cache, so a warehouse query's cost is paid per cycle; the row cap
  bounds the payload, not the query.
* **Provenance is per-artifact, not per-chunk.** An ingested document's chunks
  trace to the document, and the document to its upstream artifact — but a
  retrieved chunk does not carry the connector's name inline.
* **Deletion upstream is not propagated.** An artifact removed from SharePoint
  stops being listed, and its provenance row stops advancing, but the ingested
  document remains. Retiring it would need a governed deletion decision, which
  E12's lifecycle owns.
