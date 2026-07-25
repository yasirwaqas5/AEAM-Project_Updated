# AEAM Persistence, Migrations & Data Retention (Phase E5)

This document is the operator reference for how AEAM's durable state is
evolved and governed. It covers three things E5 introduced:

1. The **migration mechanism** (Alembic) and how to run it per environment.
2. The **hot-path indexes** added for predictable query performance.
3. The **retention posture** for every persistent store (MEM-6).

It supersedes the old "no migration tool — apply schema changes by hand"
guidance. Hand-applied DDL is no longer the mechanism; migrations are.

---

## 1. Migrations (Alembic)

Alembic is the **single schema truth** for AEAM (COMPAT-5). It is the
native companion of the SQLAlchemy the platform already ships — no new
framework surface, no ORM adopted (TECH-1/TECH-2). Schema DDL continues
to be explicit SQL; migrations are hand-written, reviewed revisions
under `migrations/versions/`.

### Layout

| Path | Purpose |
|------|---------|
| `alembic.ini` | Alembic config. Deliberately contains **no** database URL. |
| `migrations/env.py` | Resolves `DATABASE_URL` from the same `Settings` the app uses (or `-x db_url=…`). Enables `render_as_batch` so one revision works on both PostgreSQL and SQLite. |
| `migrations/versions/0001_baseline_schema.py` | Frozen snapshot of the pre-E5 schema (all core + enterprise tables). |
| `migrations/versions/0002_hot_path_indexes.py` | The E5 hot-path indexes (see §2). |
| `migrations/versions/0003_policy_embedding_column.py` | E6 additive `policies.embedding` / `embedding_model` columns. |

### Running migrations

```bash
# Apply all pending migrations (production/staging deploy step):
alembic upgrade head

# Show what is currently applied:
alembic current

# Revert the most recent migration:
alembic downgrade -1

# Full revision graph:
alembic history --verbose

# Against a specific DB without touching env/.env (tests, one-offs):
alembic -x db_url=postgresql://user:pass@host/db upgrade head
```

The connection URL is resolved in this precedence order (see
`migrations/env.py`): `-x db_url=…` → `DATABASE_URL` env var →
`Settings().DATABASE_URL`. There is exactly one source of connection
truth and **no credential is ever written into a tracked file** (SEC-5).

### Adopting migrations on an existing (already-populated) database

The baseline revision is written with `CREATE TABLE IF NOT EXISTS`, so
you can adopt Alembic on a database that predates it without rebuilding:

```bash
# Tell Alembic the baseline is already present, then apply the rest:
alembic stamp 0001_baseline
alembic upgrade head
```

### Relationship to the startup `CREATE IF NOT EXISTS` path

`DatabaseClient._create_tables_if_not_exist()` and
`create_enterprise_tables()` remain as a **dev-only convenience** so a
developer can start against a fresh SQLite/PostgreSQL DB with zero setup.
They are labelled as such in code. A migration-built database and the
startup-DDL database are **schema-identical** — this is asserted by
`aeam/tests/test_phase_e5_migrations.py::test_migrated_schema_matches_startup_ddl`,
which fails the build if the two ever drift. Any schema change must land
in **both** a migration revision and the startup DDL.

### CI

The gating CI (`.github/workflows/deploy.yml`) runs the migration
up/down tests against PostgreSQL (the production dialect) as part of the
normal `pytest` run, so a broken or non-reversible migration fails the
pipeline.

---

## 2. Hot-path indexes

The 2026-07 audit flagged two unbounded, unindexed hot reads. Migration
`0002_hot_path_indexes` adds:

| Index | Table | Serves |
|-------|-------|--------|
| `idx_incidents_timestamp` | `incidents (timestamp DESC)` | `GET /api/v1/incidents/` and every console page + observability summary that consumes that list |
| `idx_metrics_metric_timestamp` | `metrics (metric, timestamp DESC)` | `DatabaseClient.fetch_metric_history()` — ForecastAgent + Adaptive Detection, per investigation |
| `idx_audit_logs_timestamp` | `audit_logs (timestamp DESC)` | audit history queries by time window (E3 durable sink) |
| `idx_audit_logs_user_id` | `audit_logs (user_id)` | audit history queries by principal |
| `idx_action_logs_executed_at` | `action_logs (executed_at DESC)` | `GET /api/v1/logs/agents` |

These are additive: creating an index changes no row and no query result,
only the plan used to produce it.

---

## 3. Retention posture (MEM-6)

Every persistent store has a **declared owner and retention posture**.
E5 declares them; enforcement tooling (automated purges) may follow in a
later phase — the declaration is the E5 deliverable.

| Store / table | Owner | Growth driver | Retention posture | Enforcement today |
|---------------|-------|---------------|-------------------|-------------------|
| `incidents` (PostgreSQL) | Platform / SRE | one row per finalized investigation | **Retain indefinitely** — the system-of-record. Archive to cold storage beyond ~24 months if volume demands. | Manual; indexed for query at volume (E5). Console reads are bounded (E6). |
| `metrics` (PostgreSQL) | Platform / SRE | one row per observed KPI sample | **Rolling window** — only recent history feeds ForecastAgent/AdaptiveDetection. Safe to prune beyond `FORECAST_MIN_HISTORY_DAYS` × a safety factor (recommended ≥ 90 days). | Manual prune; `fetch_metric_history(limit=…)` already bounds reads (E1). |
| `decisions` (PostgreSQL) | Platform / SRE | one row per decision | Retain with the parent incident. | Manual. |
| `action_logs` (PostgreSQL) | Security / Compliance | one row per external action attempt | **Retain ≥ 12 months** (operational audit). | Manual; indexed by `executed_at` (E5). |
| `audit_logs` (PostgreSQL) | Security / Compliance | one row per authenticated request | **Retain per compliance policy (≥ 12 months typical)**. Durable sink introduced in E3. | Manual; indexed by `timestamp` + `user_id` (E5). |
| `sources` / `documents` / `datasets` / `schemas` / `versions` / `ingestion_jobs` (PostgreSQL) | Knowledge Ops | one row per registered asset / job | Retain while the asset is active; lifecycle governance (retire/expunge) lands in E12. | Manual. |
| `policies` (PostgreSQL) | Knowledge Ops | one row per extracted policy | Retain while the source document is active; policy lifecycle (active/retired) lands in E12. | Manual. |
| Qdrant collections (`aeam_documents`, `aeam_incident_memories`) | Knowledge Ops | one point per chunk / remembered incident | Retain with the owning document/incident; memory correction/expunge lands in E12 (MEM-4). | Manual. |
| Redis (dedup, idempotency, rate-limit, ingestion queue) | Platform / SRE | ephemeral | **TTL-governed** — every key already carries a TTL; nothing is retained indefinitely. | Automatic (TTL). |
| BlobStore (local dir or S3 bucket) | Knowledge Ops | one object per unique uploaded file | Retain while any `versions.blob_ref` references it; content-addressed, so dedup keeps it bounded. | Manual. |
| Prometheus metrics | Platform / SRE | in-process counters | **Windowed** — the `/metrics` scrape is point-in-time; retention is the scraper's concern, not AEAM's. | Scraper-side. |
| Forecast model artifacts (`FORECAST_MODEL_DIR`) | Platform / SRE | one file per metric | Overwritten on retrain (`FORECAST_RETRAIN_DAYS`); no unbounded growth. | Automatic (overwrite). |

**Ownership legend:** *Platform/SRE* run the datastores; *Security/Compliance*
own the audit surfaces; *Knowledge Ops* own ingested knowledge and policies.

This table is the MEM-6 declaration referenced by the E5 acceptance
criteria. When a store's posture changes, update this table in the same
change.
