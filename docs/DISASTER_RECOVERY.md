# AEAM Backup, Restore & Disaster Recovery

**Phase E13 — Enterprise Certification.** Article XVI requires a declared
retention and backup/restore posture for every store (MEM-6), and E13 requires
that a full restore drill **succeeds and is documented** — not that a runbook
exists. This document is the runbook; `scripts/dr_drill.py` is the executable
rehearsal; `aeam/tests/test_phase_e13_certification.py` runs that rehearsal on
every CI build.

Companion: [`docs/persistence_and_retention.md`](persistence_and_retention.md)
(what each store holds and for how long),
[`docs/ENTERPRISE_CERTIFICATION.md`](ENTERPRISE_CERTIFICATION.md),
[`docs/SRE_RUNBOOK.md`](SRE_RUNBOOK.md).

---

## 1. Store inventory and posture

| Store | Contents | Authoritative? | Backed up | RPO target | RTO target |
|---|---|---|---|---|---|
| PostgreSQL | incidents, decisions, action logs, audit logs, approvals, datasets, documents metadata, policies | **Yes** | Yes — daily full + continuous WAL | 5 min (PITR) | 1 h |
| Object store (blobs) | uploaded source documents, content-addressed | **Yes** | Yes — provider versioning + cross-region replication | 15 min | 1 h |
| Qdrant | RAG chunks, Enterprise Memory, policy vectors | No — **derived** | Snapshots (convenience) | 24 h | 4 h (or rebuild) |
| Redis | dedup windows, idempotency markers, rate-limit counters, cached retrievals, ingestion queue | No — **transient** | **No, by decision** | n/a | immediate (empty) |
| Forecast model artifacts | trained Prophet models | No — **rebuildable** | Optional | 24 h | retrain |
| Configuration | `.env` / environment variables / Secret Manager | Yes | Via the deployment manifest in version control | n/a | redeploy |

### Why Redis is deliberately not backed up

Every Redis key is either reconstructible (cached retrievals, BM25 warm state)
or intentionally short-lived (dedup windows, idempotency markers, rate-limit
counters). **Restoring a stale dedup window would suppress real incidents** —
an empty cache is the safer recovery state. This is a stated posture, not an
omission; `scripts/dr_drill.py` records it in every drill's evidence record so
the declaration cannot quietly disappear.

### Why Qdrant is derived, not authoritative

Collections are rebuildable by re-ingesting from the object store and
re-embedding. Losing them costs embedding time, not data. Snapshots exist to
save that time, so an unreachable Qdrant during a drill is an honest `skipped`,
never a failure and never a silent pass. Note that a rebuild with a *different*
embedding model is a TECH-6 event with its own re-validation consequences
(RAG-4) — restore with the same model the corpus was indexed with.

---

## 2. Backup procedures

### 2.1 PostgreSQL

Production backup is the managed provider's mechanism — automated daily base
backups plus continuous WAL archiving for point-in-time recovery. Verify both
are enabled; an unverified backup is a hope, not a posture.

Ad-hoc logical dump before a risky change:

```bash
pg_dump --format=custom --no-owner --file aeam-$(date +%Y%m%dT%H%M).dump "$DATABASE_URL"
```

**Schema is not part of the data backup.** It is reconstructed by
`alembic upgrade head` from `migrations/versions/`, which is the single schema
truth (Phase E5). Restore order is therefore always: create database →
migrate → load data.

### 2.2 Object store

Enable bucket versioning and cross-region replication at the provider. Blobs
are content-addressed (the key *is* the SHA-256 of the bytes), so a restore is
verifiable by recomputing hashes rather than by trusting the copy — which is
exactly what the drill does.

```bash
aws s3 sync "s3://$BLOB_S3_BUCKET/$BLOB_S3_PREFIX" ./blob-backup/
```

### 2.3 Qdrant

```bash
curl -X POST "$VECTOR_DB_URL/collections/<collection>/snapshots"
```

Or let the drill do every collection at once — `snapshot_qdrant()` in
`scripts/dr_drill.py`.

### 2.4 Configuration

The deployment manifest (`deploy/cloudrun.yaml`) is in version control and is
the recovery source. Secret *values* live in the secret manager and are
recovered by its own mechanism; nothing in this repository can restore them,
and nothing in this repository contains them.

Note the honest caveat from Phase E4: on ephemeral compute,
`CONFIG_PERSISTENCE_MODE=ephemeral` means admin-API `.env` writes do **not**
survive recycle. Durable production changes go through the manifest.

---

## 3. Restore procedure

Estimated wall-clock for a full recovery: **45–75 minutes**, dominated by
database restore and (if rebuilding rather than restoring) Qdrant re-embedding.

1. **Provision** a database, object store, Redis and Qdrant. Redis starts
   empty — that is the intended state.
2. **Schema first.**
   ```bash
   DATABASE_URL=<new-dsn> alembic upgrade head
   ```
3. **Restore data.**
   ```bash
   pg_restore --no-owner --dbname "<new-dsn>" aeam-<timestamp>.dump
   ```
   For PITR, follow the provider's recovery-target procedure instead.
4. **Restore blobs.**
   ```bash
   aws s3 sync ./blob-backup/ "s3://$BLOB_S3_BUCKET/$BLOB_S3_PREFIX"
   ```
5. **Restore or rebuild Qdrant.** Restore snapshots if available; otherwise
   re-ingest from the object store with the same embedding model.
6. **Redeploy** the application pointing at the new endpoints.
7. **Verify** — the checklist in §5.

---

## 4. The rehearsal

`scripts/dr_drill.py` performs backup → restore → **verify** and writes a
machine-readable evidence record. It refuses to run when the source and the
restore target are the same database: the restore stage truncates the tables
it restores, and a drill that destroys the data it claims to protect is worse
than no drill.

```bash
python scripts/dr_drill.py \
  --backup-dir ./dr-backup \
  --restore-database-url "postgresql://user:pass@host/aeam_restore_drill" \
  --evidence dr-evidence.json
```

Exit status is the verdict: non-zero means the drill **failed**. That is the
point — a rehearsal that cannot fail proves nothing.

Verification is a SHA-256 digest over the exported table contents, computed
before backup and again after restore. Row order is normalised (it is not a
property the application depends on), but every field of every row must match.
A partial restore is detected and reported with per-table expected/actual
counts — asserted by `test_drill_detects_a_lossy_restore`.

### Recorded drill result — live infrastructure, 2026-07-28

Executed against a real PostgreSQL 16 source, a separate real PostgreSQL
restore target (`aeam_dr_drill`, schema created by `alembic upgrade head`), and
a live Qdrant instance. Source data was produced by driving three real
investigations through `POST /api/v1/trigger`, so the backed-up rows are
genuine incidents, decisions, action logs and audit rows — not fixtures.

| Stage | Result |
|---|---|
| `database.backup` | **ok** — 15 rows across 14 tables exported |
| `blobs.backup` | **ok** — 0 blobs (no documents ingested in this deployment) |
| `qdrant.snapshot` | **ok** — 2 collection snapshots created (`aeam_documents`, `aeam_incident_memories`) |
| `redis.posture` | **ok** — declared not-backed-up, with rationale |
| `database.restore` | **ok** — 15 rows restored across 14 tables |
| `database.verify` | **ok** — digest `d3b06459e210…` matched |
| Verdict | **passed** (exit 0) |

**What the live run caught.** Two real defects that the in-process suite could
not have found, both now fixed and covered by regressions:

1. The drill's table list named `approval_requests` / `approval_verdicts`; the
   actual E9 tables are `incident_approvals` / `review_verdicts`. Approval
   chains would have been silently omitted from the backup — the exact class of
   loss that releases work no human signed off on.
2. PostgreSQL returns `json`/`jsonb` columns as native Python dicts, which
   psycopg2 cannot adapt back into an INSERT. Any incident carrying findings
   would have failed to restore. The export now canonicalises them, which also
   keeps the verification digest stable across a round trip.

This is the argument for rehearsing rather than writing a runbook: both defects
lived in code that passed its own tests.

A CI-portable version of the same drill — with the object store and the
negative controls the live environment had no data for — runs on every build:
`aeam/tests/test_phase_e13_certification.py::test_full_restore_drill_succeeds_and_verifies`
(database + 25 blobs, every hash re-verified),
`test_drill_detects_a_lossy_restore` (a truncated backup is correctly reported
as failed), and `test_drill_refuses_to_restore_over_its_own_source`.

Re-run the live drill whenever the schema, the storage backend, or the
deployment topology changes — at minimum quarterly.

---

## 5. Post-recovery verification checklist

- [ ] `GET /health` returns `healthy` (not `degraded`) — database, Redis and
      worker heartbeats all reporting.
- [ ] `GET /api/v1/system/status` shows the expected agent roster.
- [ ] `GET /api/v1/incidents/?limit=50` returns the expected recent incidents.
- [ ] `GET /api/v1/audit?limit=10` returns pre-incident audit rows — proving
      the durable audit trail survived (SEC-6/ARCH-7).
- [ ] `GET /api/v1/review/queue` shows any approvals that were pending before
      the outage; none were silently released.
- [ ] A retrieval query returns cited chunks — proving Qdrant is populated and
      chunk IDs still resolve (COMPAT-7).
- [ ] `POST /api/v1/trigger` produces a complete investigation end to end.
- [ ] `GET /api/v1/system/compliance` reports the intended tenancy, data
      classification and identity postures.
- [ ] Sign-in works: SSO redirect completes, or a pasted token is accepted.

---

## 6. Failure modes short of total loss

| Symptom | Immediate action | Recovery |
|---|---|---|
| Database unreachable | `/health` reports degraded; investigations fail loudly rather than persisting nothing | Failover to replica; no application change needed |
| Redis unreachable | Dedup and idempotency fail **open** (a documented E-series trade-off, MOD-6): duplicate work is possible, suppression is not | Restart Redis; state repopulates on demand |
| Qdrant unreachable | Retrieval returns nothing and says so — there is no silent fallback path | Restore snapshot or rebuild from blobs |
| Object store unreachable | Ingestion fails; existing incidents are unaffected | Restore provider access; blobs are content-addressed and idempotent to re-put |
| IdP unreachable | Existing sessions run to token expiry; new sign-ins fail with 401 | Restore IdP; or temporarily revert to the static-key posture (see below) |
| Monitor thread dead | `/health` flips to degraded on a stale heartbeat (E7) | Restart the instance; the loop is stateless between cycles |

### Rolling back federation

If the IdP is unavailable for an extended period, set `OIDC_ENABLED=false` and
restore `JWT_PUBLIC_KEY` to return to the E3 static-key posture. This is the
documented E13 rollback. Operators then sign in by pasting a token issued by
whatever mints them for that key — the paste-token path on the login page is
retained for exactly this reason.

---

## 7. Ownership

Each store's owner is the team operating the deployment; AEAM is deployed
software, not a hosted service. Record the named owner per store alongside this
document at deployment time — MEM-6 requires an owner as well as a posture, and
this repository cannot know who that is.
