# Project knowledge — AEAM

AEAM (Autonomous Event & Agent Monitor / Autonomous Enterprise AI Agent Mesh) is a
modular monolith that autonomously detects business/KPI anomalies, investigates
them with a mesh of agents (rule + statistical detection, forecasting, RAG over a
knowledge base, LLM reasoning), and takes actions (Slack, Jira, Email, Webhooks).

- **Backend:** FastAPI app in `aeam/` (Python).
- **Frontend:** React 18 + Vite in `frontend/` (dev server proxies `/api` → `localhost:8080`).
- **Infra:** PostgreSQL + Redis + Qdrant (vector DB), all via Docker.

## Quickstart

Backend:
```bash
pip install -r requirements.txt
uvicorn aeam.main:app --reload --port 8080
```

Frontend:
```bash
cd frontend && npm install && npm run dev   # Vite dev server on :5173
```

Full stack (Postgres + Redis + Qdrant + app):
```bash
docker-compose up
```

- Test (backend): `pytest` (tests live in `aeam/tests/`)
- End-to-end demo: `python run_simulation.py`
- Frontend build / preview: `npm run build` / `npm run preview`
- No linter or type-checker is configured in this repo.

## Environment

Settings are loaded via `pydantic-settings` from env / `.env` (see `aeam/config/settings.py`).

- **Required (no defaults):** `DATABASE_URL`, `REDIS_URL`, `VECTOR_DB_URL`, `ENVIRONMENT`.
  - `ENVIRONMENT` must be one of `development` / `staging` / `production` / `test`.
- **Optional:** `SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `JIRA_URL`, `JIRA_API_TOKEN`,
  `JIRA_USER_EMAIL`, `JIRA_PROJECT_KEY`, `LLM_PROVIDER` (default `gemini`),
  `LLM_API_KEY`, `LLM_ENABLED`, `USE_MOCK_LLM`, plus many `RAG_*` / `FORECAST_*`
  feature flags (all documented inline in `settings.py`).
- `Settings` uses `extra="forbid"` — an unknown env var in `.env` will fail startup.

## Architecture

Key directories (`aeam/`):
- `main.py` — entry point; the FastAPI lifespan wires every client and agent. Read this first.
- `agents/` — `orchestrator/` (core investigation loop, hybrid rules+LLM decisions),
  `monitor/`, `kpi/` (rule engine + statistical detector), `forecast/` (Prophet),
  `rag/` (ingestion + layered retrieval pipeline), `action/`, `report/`.
- `api/` — FastAPI routers (`incidents`, `trigger`, `system`, `logs`, `ingest`,
  `knowledge`, `data_center`, `retrieval_debug`).
- `core/` — `event_bus`, `event_models`, `priority_queue`, `deduplication`, `idempotency`, `state_machine`.
- `integrations/` — `database`, `redis_client`, `vector_db`, `embedding_service`, `secret_manager`.
- `intelligence/` — dataset intelligence, policy extraction/registry, cross-dataset & adaptive detection.
- `memory/`, `ingestion/`, `pipelines/`, `registry/`, `security/`, `monitoring/`, `connectors/`, `storage/`.

Data flow:
`Trigger → EventBus → Orchestrator → DecisionEngine → EvaluationEngine → ActionAgent (Slack/Jira) → DB`

RAG retrieval is a stack of composable, feature-flagged pipeline wrappers, each
falling back to the prior stage if construction fails (see `main.py` lifespan):
dense → hybrid (BM25 + RRF) → multi-query → cross-encoder rerank → evidence
diversity → advanced retrieval (entity extraction + metadata filtering +
business-relevance ranking, Phase C6).

## Conventions

- **Composition over duplication:** new capabilities wrap existing pipelines/engines
  and reuse the *same* shared instances (one `EmbeddingService`, one `QdrantClient`,
  one `LLMService`, one `long_term_memory`, one `dataset_activation`, etc.). Follow this.
- **Feature flags + graceful fallback:** each RAG stage is gated by a `RAG_*` setting
  and wrapped in try/except that logs and falls back so startup never breaks.
- **Backwards-compatible additions:** new params default to `None`/no-op so prior
  behaviour is preserved exactly when a feature isn't wired.
- **Honesty over fabrication:** never fabricate evidence/values; surface "explicitly
  unavailable" rather than reconstructing (e.g. Retrieval Explorer, no-context RAG results).
- Windows dev shell: use `dir`, `del`, `move`, `findstr` (not `ls`/`rm`/`mv`/`grep`).

## Agent reference

All agents are constructed and wired in `aeam/main.py`'s lifespan; dependencies
are injected (no globals). Each has a strict responsibility boundary noted below.

### Orchestrator — `aeam/agents/orchestrator/orchestrator.py`
Central coordinator of the incident lifecycle: `EVENT_RECEIVED → INVESTIGATING → DECIDING → COMPLETE`.
- Registered as the `"ALL"` wildcard EventBus handler; `handle_event()` is the entry point.
- `investigate()` runs one pass: DecisionEngine decides, then (idempotently, once per incident) gathers advisory findings from Enterprise Memory (C1), Policy Registry (C3), Cross-Dataset Intelligence (C4), Adaptive Detection (C5), and RAG (Phase 4); forces LLM reasoning at depth ≥ 3.
- `evaluate()` routes on EvaluationEngine result: CONTINUE (recurse) / STOP / ESCALATE.
- `finalize_incident()` executes the event's safe runbook via ActionAgent, sends structured Slack/Jira/email notifications, writes a consolidated `audit_summary`, persists via LongTermMemory, and remembers the incident.
- Constraints: no detection logic, no direct DB writes (delegates to LTM), no external API calls (delegates to ActionAgent). Advisory findings NEVER feed back into RuleEngine/DecisionEngine/ActionAgent.
- Key collaborators: `decision_engine`, `evaluation_engine`, `state_machine`, `investigation_status`, `notifications`, `runbooks`, `cause_quality`.

### Monitor — `aeam/agents/monitor/monitor_agent.py`
Deterministic KPI detection + event creation; runs a continuous polling loop in a background thread (`start()`).
- `process_kpi()` cleans history, then applies RuleEngine + StatisticalDetector + ForecastAgent; ANY signal (≥1) creates an immutable `Event`, dedups, queues, and publishes on the EventBus.
- Severity from signal count: ≥2 → HIGH, 1 → MEDIUM, 0 → LOW.
- `_run_cycle()` pulls rows from the injected `KPIRowSource` (a `CompositeKPISource`: Sheets + activated datasets) for every `rule_engine.loaded_domains` metric; persists observations to LTM so ForecastAgent has training history. No-op tick when no source is wired.
- Constraints: no LLM, no orchestrator logic, no direct DB access, no external APIs.

### KPI detection — `aeam/agents/kpi/`
There is no standalone "KPI agent" class; KPI logic is the two deterministic detectors MonitorAgent composes (plus a placeholder in the orchestrator).
- `rule_engine.py` (`RuleEngine`): loads thresholds from `aeam/config/detection_rules.yaml`; `evaluate(metric, current, previous)` dispatches by domain (`sales`, `complaints`, `inventory`). Fails fast on missing config; unknown domains return non-triggered. `loaded_domains` drives MonitorAgent's cycle. `CompositeRuleEngine` wraps it to add dynamic dataset domains.
- `statistical_detector.py` (`StatisticalDetector`): pure rolling-window detector (winsorized moving average, z-score with threshold 3.0, p5/p95 bounds). No I/O, no external libs.
- Note: `Orchestrator._run_kpi_investigation_placeholder()` writes synthetic "Simulated root cause" evidence — a placeholder, not real analysis (see Gotchas).

### Forecast — `aeam/agents/forecast/forecast_agent.py`
Per-metric Prophet model lifecycle + deviation detection.
- `load_or_train(metric)`: loads a fresh model (`< 7 days`) from `models/forecasting/` (path is relative to the working directory; the repo keeps files at `aeam/models/forecasting/`), else fetches history from LTM (`HistoricalDataSource` protocol), preprocesses, trains, saves. Returns `{"insufficient_data": True}` if fewer than 30 rows.
- `analyze(metric, actual_value)`: predicts next period and returns `{predicted, lower_bound, upper_bound, is_deviation, deviation_percent}`. Always returns a dict (never raises).
- Constraints: never creates Events, never calls LLM/ActionAgent, never retrains in the background — training is explicit only.

### RAG — `aeam/agents/rag/rag_agent.py`
Research-only retrieval-augmented investigation. `investigate(event, memory)`:
- Formulates a natural-language query (deterministic, non-hallucinating rewrite/broaden across up to 3 attempts: `original` → `rewritten` → `broadened`); extracts entities → `filter_criteria` when an `entity_extractor` is wired (Phase C6).
- Retrieves via the injected (fully composed) retrieval pipeline, assembles a strict grounded prompt (context-only, chunk_id citations required), calls the LLM (temp 0.2, max 1000 tokens), parses JSON resiliently (`parse_llm_json`), and validates grounding (`RAGResponseValidator`).
- Returns `{findings, confidence, memory_updates}`; surfaces failures as structured `error` fields rather than raising. Never decides STOP/CONTINUE, never writes to DB, never calls external APIs, never mutates the Event.
- Retrieval is a layered pipeline (see Architecture) built in `main.py`; `RetrievalDebugTracer` mirrors it read-only for the Retrieval Explorer.

### Action — `aeam/agents/action/action_agent.py`
The ONLY component permitted to call external APIs. `execute(action_type, parameters, incident_id)`:
- Dispatches through an internal registry (`slack`, `email`, `webhook`, `sheets`, `diagnostics`, `monitoring`, and `jira` when configured).
- Enforces idempotency (24h TTL via Redis), retries (max 2 attempts, exponential backoff + jitter), a per-action-type `CircuitBreaker`, and audit logging to the `action_logs` table.
- Never raises on handler failure — returns `{status, action_id, result, ...}` with status `SUCCESS`/`FAILED`/`ALREADY_EXECUTED`/`CIRCUIT_OPEN`. Constraints: no LLM, no decision/orchestrator logic.

### Report — `aeam/agents/report/report_agent.py`
Content-generation only; reads ShortTermMemory (never mutates it), fills templates from `aeam/templates/`.
- `generate_report(memory)` → `{executive_summary, detailed_report, confidence}`; `generate_alert(memory)` → `{message, severity, event_type}`.
- Works fully in deterministic fallback mode when no LLM is injected; with an LLM (temp 0.4, max 1200 tokens) it enriches the narrative.
- Appends advisory sections (Matched Policies C3, Cross-Dataset C4, Adaptive Detection C5) as additive post-processing that never alters root cause/confidence.
- Constraints: no action execution, no decisions, no DB writes, no external APIs; always returns a valid dict (never raises).

## Gotchas

- **401 on `/api` in dev is expected:** `SecurityMiddleware` bypasses ALL JWT/RBAC/
  rate-limit checks when `ENVIRONMENT=development`. Never make the bypass unconditional
  (no `or True`) — that would disable auth in production too. Never deploy with `ENVIRONMENT=development`.
- **Scheduler is disabled:** `scheduler.add_job` / `scheduler.start` are commented out
  in `main.py`. The "24/7 autonomous" loop does not run; events enter only via
  `POST /api/v1/trigger` or `run_simulation.py`. Don't rely on the scheduler in e2e tests.
- **No DB migration tool:** apply schema changes by hand. Confirm columns with
  `\d incidents` before editing SQL in `incidents.py`, e.g.
  `docker exec -it aeam-postgres psql -U postgres -c "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS llm_response TEXT;"`
- **Qdrant has no fallback:** if RAG retrieval returns nothing, confirm Qdrant is
  reachable at `VECTOR_DB_URL`. `test_phase4_rag.py` hits a *real* Qdrant (not mocked).
- **Placeholders are not ground truth:** `_run_kpi_investigation_placeholder` in
  `orchestrator.py` ("Simulated root cause") and `expected_value = value * 2` in
  `trigger.py` are placeholders — never reuse them in real detection paths.
- **Case-sensitive imports:** file/import casing matters on Linux/Docker (not Windows);
  e.g. a blank Agents page usually means an `AgentLogCard.jsx` import-casing mismatch.
