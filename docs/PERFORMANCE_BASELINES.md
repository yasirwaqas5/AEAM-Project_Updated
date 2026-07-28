# AEAM Performance Baselines

**Phase E13 — Enterprise Certification.** "Scale limits are known numbers" is
one of the four things a vendor review asks for. This document records what was
measured, how, and what budget each measurement now has to stay inside.

The budgets themselves live in `aeam/tests/fixtures/performance_budgets.json`
— one machine-readable file, versioned with the code — and are asserted by
`aeam/tests/test_phase_e13_performance.py`, gated by the `performance` job in
`.github/workflows/deploy.yml`.

---

## 1. Methodology

Four axes, one per subsystem the roadmap names for E13:

| Axis | Measures | Phase it protects |
|---|---|---|
| Concurrent investigation throughput | `Orchestrator.handle_event()` across worker threads, dispatch → `finalize_incident()` | E2 (per-incident state isolation) |
| Ingestion throughput | `extract_text()` + `TextChunker.chunk_text()` | B1.3 ingestion pipeline |
| Console responsiveness at volume | `GET /api/v1/incidents/` against a year of incidents | E6 (pagination and scale contracts) |
| Autonomous-loop cycle stability | Poll-loop cadence and heartbeat freshness across cycles | E7 (supervised background workers) |

**What is deliberately excluded.** No budget measures LLM latency, embedding
latency, or a live database server. Those are properties of a model provider,
a model, and a managed service respectively — not of AEAM's code — and a
budget that moves when a provider has a slow afternoon teaches nothing. Every
figure below is AEAM's own work: `LLM_ENABLED=false`, in-process SQLite, no
network.

**Why the budgets are loose.** They are CI ceilings, not observed averages.
The reference measurements were taken on an 8-core workstation; GitHub-hosted
runners are 2-core and shared. Budgets carry an order of magnitude of headroom
in places so the suite fails on a *structural* regression — an accidental
O(n²), a per-row query inside a loop, a lost index — and not on runner noise.
Every budget records its `observed_local` figure alongside the ceiling, so the
headroom is visible rather than implied, and any future tightening can be
argued from real numbers rather than intuition.

**Measurement semantics (OBS-2).** Every budget in the fixture declares
`what`, `why`, `source`, `window`, and `observed_local`. A budget added later
without those fails `test_budget_file_declares_its_measurement_semantics` —
the guard exists because a published number without its window and source is
exactly the kind of metric Article XI was written against.

---

## 2. Reference environment

| | |
|---|---|
| Recorded on | 2026-07-28 |
| Hardware | 8-core x86-64 developer workstation, 16 GB RAM |
| Python | 3.11 |
| Database | SQLite, file-backed — the CI-portable substitute for PostgreSQL |
| LLM | disabled |
| External services | none (no Redis, Qdrant, or object store in the measured paths) |

---

## 3. Recorded baselines

### 3.1 Concurrent investigation throughput

16 events across 8 worker threads, each a full investigation through the
Orchestrator to finalization.

| | Observed | Budget |
|---|---|---|
| Total wall clock | 0.010 s | ≤ 30.0 s → **10.0 s** |
| Throughput | 1,657 events/s | ≥ **2.0 events/s** |

The suite also asserts that all 16 investigations actually finalized —
throughput over dropped work is not throughput. Phase E2 proved these
investigations do not leak state into one another; this budget proves that
isolation did not cost anything measurable.

**Interpretation for capacity planning:** the orchestration layer is not the
bottleneck at any realistic incident rate. In a production deployment the
limiting factors are, in order: LLM provider latency (seconds per call),
embedding/retrieval round trips, and database write throughput — none of which
this figure includes.

### 3.2 Ingestion throughput

25 markdown documents of 40 KB each, extracted and chunked.

| | Observed | Budget |
|---|---|---|
| Total wall clock | 0.018 s | ≤ **10.0 s** |
| Throughput | 1,356 documents/s | ≥ **5.0 documents/s** |

Embedding is excluded (TECH-6: a model property). Real end-to-end ingestion
throughput is embedding-bound; this budget guards the part AEAM owns, so a
regression in extraction or chunking cannot hide behind model latency.

### 3.3 Console responsiveness at a year of incident volume

5,000 incidents — roughly a year at 14 incidents/day — seeded into a real
table behind the endpoint the console actually calls.

| | Observed | Budget |
|---|---|---|
| Seed (setup, not budgeted) | 0.624 s | — |
| Paged request (`limit=50`), worst of 5 | 0.012 s | ≤ **2.0 s** |
| Unpaged request (all 5,000 rows) | 0.258 s | ≤ **10.0 s** |

Both paths are budgeted deliberately. The paged path is what the console uses.
The unpaged path is the parameter-less call E6 preserved for backward
compatibility (COMPAT-2), and budgeting it means that compatibility path
cannot silently become unusable at volume — the failure mode where "we kept it
working" quietly stops being true.

The paged assertion also verifies the response is genuinely bounded by
`limit`, so a regression that ignores pagination fails on correctness before
it fails on time.

### 3.4 Autonomous-loop cycle stability

10 consecutive cycles at a 50 ms interval, with heartbeat ages sampled per
cycle.

| | Observed | Budget |
|---|---|---|
| Worst cycle / interval | 1.02× | ≤ **6.0×** |
| Worst heartbeat age | 0.0002 s | ≤ **5.0 s** |

Drift is measured as a ratio rather than an absolute so the budget stays valid
if the interval is retuned. A loop that stalls fails the cycle-count assertion
before it reaches the timing ones — which is the failure mode Phase E7's
supervision exists to catch: a dead monitor thread must be *detected*, not
discovered.

---

## 4. Known limits and untested scale

Stated rather than left to be discovered:

- **Single-node modular monolith.** Cloud Run `maxScale=5` is safe because
  deduplication and idempotency are Redis-shared and the Orchestrator is
  reentrant (E2), but no test measures cross-instance contention.
- **Investigation is synchronous.** `POST /api/v1/trigger` returns only after
  finalization. Under a burst, request concurrency is bounded by the worker
  count, not by a queue. This is a documented, deliberate trade-off (MOD-6).
- **Retrieval latency is not budgeted here.** It depends on Qdrant sizing and
  the embedding model. `docs/retrieval_debugging.md` covers per-query
  inspection, and the E12 golden-set evaluation covers retrieval *quality*
  regression separately from speed.
- **Frontend rendering at volume is not measured.** The API budget guarantees
  the data arrives bounded and fast; browser rendering of very large tables
  was not profiled.
- **Above ~50,000 incidents** the unpaged compatibility path will approach its
  budget on modest hardware. The paged path is the one the console uses and it
  is index-backed (Phase E5 hot-path indexes).

---

## 5. Re-recording the baselines

Re-run the suite and update both the ceilings and the `observed_local`
figures whenever the reference hardware changes, a measured code path is
substantially rewritten, or a budget starts failing for a reason that turns
out to be legitimate rather than a regression:

```bash
python -m pytest aeam/tests/test_phase_e13_performance.py -v
```

Record the new numbers in `aeam/tests/fixtures/performance_budgets.json`
together with the date and hardware in `baseline_environment`. **Never widen a
budget to make a red build green without first establishing why the number
moved** — that converts a working gate into decoration.
