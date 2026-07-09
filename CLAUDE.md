# CLAUDE.md — AEAM Project

## Quick Start
```bash
# Backend
pip install -r requirements.txt
uvicorn aeam.main:app --reload --port 8080

# Frontend
cd frontend && npm install && npm run dev
```

## Project Architecture
- Backend: FastAPI app in `aeam/` with agents (Orchestrator, Monitor, Action, RAG, Forecast)
- Frontend: React+Vite in `frontend/`, proxies `/api` → `localhost:8080`
- Database: PostgreSQL + Redis (Docker)

Key files:
- `aeam/main.py` — Entry point, wires all agents
- `aeam/agents/orchestrator/orchestrator.py` — Core loop
- `aeam/middleware/security_middleware.py` — Auth bypass in dev
- `aeam/services/llm_service.py` — LLM integration
- `aeam/api/incidents.py` — Incidents API
- `aeam/api/trigger.py` — Manual event trigger

## Environment Variables (`.env`)
Required: `DATABASE_URL`, `REDIS_URL`, `ENVIRONMENT`, `VECTOR_DB_URL`
Optional: `SLACK_BOT_TOKEN`, `JIRA_URL`, `JIRA_API_TOKEN`, `LLM_PROVIDER`, `LLM_API_KEY`

`ENVIRONMENT` has no default — it must be one of `development` / `staging` / `production` / `test`. It also controls `SecurityMiddleware`: when set to `development`, **all** JWT/RBAC/rate-limit checks are bypassed. Never run a deployed instance with `ENVIRONMENT=development`.

## Known Issues & Quick Fixes
- **401 on /api in dev** → expected: `SecurityMiddleware` bypasses auth entirely when `ENVIRONMENT=development`. To test authenticated flows locally, issue a real JWT. Never make the bypass unconditional (e.g. `or True`) — that disables auth in every environment, including production.
- **Scheduler not firing** → `scheduler.add_job` / `scheduler.start` are commented out in `main.py`. The "24/7 autonomous" loop is currently disabled; events only enter the pipeline via `POST /api/v1/trigger` or `run_simulation.py`. Re-enable only after load-testing `MonitorAgent`.
- **Hardcoded values in alerts** → `_run_kpi_investigation_placeholder` in `orchestrator.py` must read from `self._active_event`, never hardcode metric/value data. It's an explicit placeholder ("Simulated root cause") pending a real KPI Agent — don't treat its output as ground truth.
- **Blank Agents page** → check `frontend/src/components/AgentLogCard.jsx` exists and the import in `Agents.jsx` matches exactly (case-sensitive on Linux/Docker, unlike Windows).
- **Missing DB column** → there is no migration tool in this repo; run `\d incidents` to confirm the actual missing column, then apply by hand, e.g.:
  `docker exec -it aeam-postgres psql -U postgres -c "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS llm_response TEXT;"`
- **`expected_value` missing in trigger.py** → currently defaults to `payload.value * 2`, a placeholder baseline used only for manually-triggered events. Never reuse this formula in real detection paths (KPIAgent/MonitorAgent) — real expected values must come from statistical baselining, not a fixed multiplier.
- **Incidents returns []** → the SQL in `incidents.py` must match actual DB columns; run `\d incidents` before editing the query.
- **RAG retrieval returns nothing** → confirm Qdrant is reachable at `VECTOR_DB_URL`; there is no fallback path if the connection fails.

## Data Flow
Trigger → EventBus → Orchestrator → DecisionEngine → EvaluationEngine → ActionAgent (Slack/Jira) → DB

## Agents
- MonitorAgent: Watches KPI changes, applies rule-based checks
- KPIAgent: Statistical anomaly detection (Z-score, moving average)
- ForecastAgent: Time-series prediction (Prophet)
- RAGAgent: Document retrieval + LLM reasoning
- Orchestrator: Coordinates investigation, hybrid decision (rules + LLM)
- ActionAgent: Executes Slack, Jira, Email, Webhooks
- ReportAgent: Generates human-readable summaries

## Testing
- `pytest` for backend tests
- `python run_simulation.py` for end-to-end demo
- Manual trigger: `curl -X POST http://localhost:8080/api/v1/trigger/ ...`
- RAG tests (`test_phase4_rag.py`) hit a real Qdrant instance at `VECTOR_DB_URL` — not mocked. Ensure it's running before running that suite.
- The scheduler is disabled (see Known Issues) — don't rely on it in end-to-end tests. Drive events via `/api/v1/trigger` or `run_simulation.py` instead.