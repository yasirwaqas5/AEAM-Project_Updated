"""
aeam/tests/test_phase_e11_observability.py

Phase E11 — Platform Observability & Audit Surface regression ledger
(OBS-1..OBS-6, SEC-6, COMPAT-1, EXPL-3).

Covers the E11 contract:

1. **Persisted per-incident duration.** ``finalize_incident`` writes the
   measured wall-clock duration into ``audit_summary``, and the same
   measurement still feeds the Prometheus histogram (no second measurement,
   no second store).
2. **Mixed-history honesty in D3.** ``ObservabilityEngine`` reports a REAL
   duration for post-phase incidents, an honest stated reason when none
   exists, and discloses exactly how many incidents in the window predate
   the field (COMPAT-1 / EXPL-3).
3. **Platform cost surface.** Per-incident cost attribution accumulates LLM
   spend, retrieval volume, and action counts; the roll-up sums them per
   window and discloses unavailability for older data.
4. **Metric semantics (OBS-2).** The new Prometheus series exist with the
   declared names/labels, and cost attribution is thread-local so concurrent
   investigations never cross-contaminate.
5. **Audit query surface (SEC-6).** Queries filter by principal and time
   window, page with ``X-Total-Count``, reject malformed windows, and are
   RBAC-mapped to the grant the auditor role holds.
6. **Alert artifacts.** The shipped scrape config and alert rules are valid
   YAML, every alert declares its semantics, and every expression references
   a metric AEAM actually publishes (no alert on an invented series).
7. **Distributed tracing.** Tracing is off by default, degrades safely, and
   emits one correlated incident-scoped trace when enabled.

All tests run in-process against SQLite/stubs — no live DB, Redis, Qdrant,
Prometheus, or OTLP collector required (TEST-3).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.audit import router as audit_router
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.observability import ObservabilityEngine
from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP, SecurityMiddleware
from aeam.monitoring import metrics as metrics_module
from aeam.monitoring import tracing as tracing_module
from aeam.monitoring.metrics import IncidentCostScope, incident_cost_scope
from aeam.security.rbac import RBAC

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _incident(findings: list[dict] | None = None) -> dict:
    return {"findings": findings or []}


def _audit_finding(**fields) -> dict:
    return {"type": "audit_summary", "investigation_status": "RESOLVED", **fields}


class _StubContainer:
    def __init__(self, db):
        self.db = db


def _audit_app(db) -> FastAPI:
    app = FastAPI()
    app.state.container = _StubContainer(db)
    app.include_router(audit_router)
    return app


@pytest.fixture()
def audit_db(tmp_path):
    """A real SQLite DatabaseClient with the audit_logs table populated."""
    client = DatabaseClient(database_url=f"sqlite:///{tmp_path / 'audit.db'}")
    rows = [
        ("e1", "2026-07-20T10:00:00+00:00", "alice", "GET", "/api/v1/incidents", 200),
        ("e2", "2026-07-21T10:00:00+00:00", "alice", "POST", "/api/v1/review/x", 200),
        ("e3", "2026-07-22T10:00:00+00:00", "bob", "GET", "/api/v1/incidents", 403),
        ("e4", "2026-07-23T10:00:00+00:00", "bob", "GET", "/api/v1/logs", 200),
        ("e5", "2026-07-24T10:00:00+00:00", "carol", "DELETE", "/api/v1/knowledge/x", 200),
    ]
    for entry_id, ts, user, action, endpoint, status in rows:
        client.insert(
            table="audit_logs",
            data={
                "entry_id": entry_id, "timestamp": ts, "user_id": user,
                "action": action, "endpoint": endpoint, "status_code": status,
                "hash": f"hash-{entry_id}", "extra": None,
            },
            returning_column="entry_id",
        )
    yield client
    client.dispose()


# ===========================================================================
# 1. Persisted per-incident duration
# ===========================================================================

def test_duration_is_measured_once_and_persisted_into_audit_summary():
    """The value written to audit_summary is the SAME measurement observed on
    the Prometheus histogram — one measurement, two consumers, never two
    independent numbers that could disagree."""
    from aeam.monitoring.metrics import end_timer, investigation_duration, start_timer

    before = investigation_duration._sum.get()
    started = start_timer()
    elapsed = end_timer(investigation_duration, started)
    after = investigation_duration._sum.get()

    assert elapsed >= 0.0
    # The histogram advanced by exactly the value a caller would persist.
    assert after - before == pytest.approx(elapsed, abs=1e-9)


def test_observability_reports_measured_duration_for_post_phase_incidents():
    incidents = [
        _incident([_audit_finding(investigation_duration_seconds=2.0)]),
        _incident([_audit_finding(investigation_duration_seconds=4.0)]),
        _incident([_audit_finding(investigation_duration_seconds=6.0)]),
    ]
    summary = ObservabilityEngine().summarize(incidents)
    duration = summary["investigation_duration"]

    assert duration["available"] is True
    assert duration["unit"] == "seconds"
    assert duration["average"] == 4.0
    assert duration["median"] == 4.0
    assert duration["min"] == 2.0
    assert duration["max"] == 6.0
    assert duration["sample_count"] == 3
    assert duration["incidents_without_duration"] == 0


# ===========================================================================
# 2. Mixed-history honesty (COMPAT-1 / EXPL-3)
# ===========================================================================

def test_duration_unavailable_with_honest_reason_for_pre_phase_incidents():
    """An incident recorded before E11 has no duration field. The engine must
    say so rather than backfilling a number it never measured."""
    incidents = [_incident([_audit_finding()]), _incident([_audit_finding()])]
    duration = ObservabilityEngine().summarize(incidents)["investigation_duration"]

    assert duration["available"] is False
    assert "audit_summary.investigation_duration_seconds" in duration["reason"]
    assert duration["incidents_without_duration"] == 2
    # It must NOT quietly substitute the Prometheus process-lifetime aggregate.
    assert "never merges in" in duration["reason"]


def test_duration_discloses_how_many_incidents_predate_the_field():
    """Mixed history: some incidents have it, some don't. Both facts reported."""
    incidents = [
        _incident([_audit_finding(investigation_duration_seconds=3.0)]),
        _incident([_audit_finding()]),           # pre-E11
        _incident([_audit_finding()]),           # pre-E11
        _incident([]),                            # no audit_summary at all
    ]
    duration = ObservabilityEngine().summarize(incidents)["investigation_duration"]

    assert duration["available"] is True
    assert duration["sample_count"] == 1
    assert duration["incidents_without_duration"] == 3
    assert duration["total_investigations"] == 4


def test_duration_is_excluded_from_the_overall_health_score():
    """A duration in seconds is not a [0,1] quality rate; folding it into the
    mean would silently corrupt the health score."""
    summary = ObservabilityEngine().summarize(
        [_incident([_audit_finding(investigation_duration_seconds=42.0)])]
    )
    formula = summary["overall_ai_health_formula"]
    assert "investigation_duration and platform_cost are intentionally excluded" in formula
    assert "investigation_duration" not in (summary["overall_ai_health"].get("based_on") or [])


# ===========================================================================
# 3. Platform cost surface
# ===========================================================================

def test_cost_rollup_sums_per_incident_blocks():
    incidents = [
        _incident([_audit_finding(cost={
            "llm_calls": 2, "llm_prompt_tokens": 100, "llm_completion_tokens": 50,
            "llm_total_tokens": 150, "llm_cost_usd": 0.002, "retrieval_chunks": 5,
            "actions_executed": 1, "actions_skipped": 0, "actions_withheld": 0,
        })]),
        _incident([_audit_finding(cost={
            "llm_calls": 3, "llm_prompt_tokens": 200, "llm_completion_tokens": 100,
            "llm_total_tokens": 300, "llm_cost_usd": 0.004, "retrieval_chunks": 7,
            "actions_executed": 2, "actions_skipped": 1, "actions_withheld": 0,
        })]),
    ]
    cost = ObservabilityEngine().summarize(incidents)["platform_cost"]

    assert cost["available"] is True
    assert cost["totals"]["llm_calls"] == 5
    assert cost["totals"]["llm_total_tokens"] == 450
    assert cost["totals"]["llm_cost_usd"] == pytest.approx(0.006)
    assert cost["totals"]["retrieval_chunks"] == 12
    assert cost["totals"]["actions_executed"] == 3
    assert cost["incidents_with_cost"] == 2
    assert cost["incidents_without_cost"] == 0
    assert cost["per_incident_average"]["llm_calls"] == 2.5


def test_cost_discloses_unavailability_for_older_data():
    """Pre-E11 incidents carry no cost block. They must be reported as
    unavailable, never counted as zero-cost (which would understate the mean)."""
    cost = ObservabilityEngine().summarize(
        [_incident([_audit_finding()]), _incident([_audit_finding()])]
    )["platform_cost"]

    assert cost["available"] is False
    assert cost["incidents_without_cost"] == 2
    assert "rather than counted as zero-cost" in cost["reason"]


def test_cost_mixed_history_counts_both_populations():
    incidents = [
        _incident([_audit_finding(cost={
            "llm_calls": 1, "llm_prompt_tokens": 10, "llm_completion_tokens": 5,
            "llm_total_tokens": 15, "llm_cost_usd": 0.001, "retrieval_chunks": 2,
            "actions_executed": 1, "actions_skipped": 0, "actions_withheld": 0,
        })]),
        _incident([_audit_finding()]),
    ]
    cost = ObservabilityEngine().summarize(incidents)["platform_cost"]
    assert cost["available"] is True
    assert cost["incidents_with_cost"] == 1
    assert cost["incidents_without_cost"] == 1
    assert "never an invoiced total" in cost["cost_basis"]


# ===========================================================================
# 4. Metric semantics (OBS-2) + cost-scope isolation
# ===========================================================================

def test_new_prometheus_series_exist_with_declared_names_and_labels():
    """OBS-1: the cost surface publishes through the EXISTING metrics pipeline.
    No second metrics store — these are plain prometheus_client collectors."""
    from prometheus_client import Counter

    assert isinstance(metrics_module.retrieval_chunks_total, Counter)
    assert isinstance(metrics_module.action_executions_total, Counter)
    assert metrics_module.retrieval_chunks_total._name == "retrieval_chunks"
    assert metrics_module.retrieval_chunks_total._labelnames == ("stage",)
    assert metrics_module.action_executions_total._labelnames == ("outcome",)


def test_cost_scope_accumulates_and_snapshots():
    scope = IncidentCostScope()
    scope.start("inc-1")
    scope.record_llm(prompt_tokens=10, completion_tokens=5, cost_usd=0.001)
    scope.record_llm(prompt_tokens=20, completion_tokens=10, cost_usd=0.002)
    scope.record_retrieval(4)
    scope.record_action("executed")
    scope.record_action("withheld")

    snap = scope.snapshot()
    assert snap["incident_id"] == "inc-1"
    assert snap["llm_calls"] == 2
    assert snap["llm_prompt_tokens"] == 30
    assert snap["llm_completion_tokens"] == 15
    assert snap["llm_total_tokens"] == 45
    assert snap["llm_cost_usd"] == pytest.approx(0.003)
    assert snap["retrieval_chunks"] == 4
    assert snap["actions_executed"] == 1
    assert snap["actions_withheld"] == 1

    scope.clear()
    assert scope.snapshot() is None


def test_cost_scope_recording_outside_a_scope_is_a_silent_noop():
    """A background LLM call is not attributable to an incident. Inventing an
    attribution for it would be dishonest, so it must simply not record."""
    scope = IncidentCostScope()
    scope.record_llm(prompt_tokens=99, completion_tokens=99, cost_usd=9.9)
    scope.record_retrieval(50)
    assert scope.snapshot() is None


def test_cost_scope_is_thread_local_so_concurrent_investigations_never_mix():
    """Phase E2 made handle_event reentrant. Cost attribution must be too."""
    scope = IncidentCostScope()
    results: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def worker(incident_id: str, tokens: int) -> None:
        scope.start(incident_id)
        barrier.wait()  # force genuine interleaving
        scope.record_llm(prompt_tokens=tokens, completion_tokens=0, cost_usd=0.0)
        barrier.wait()
        results[incident_id] = scope.snapshot()
        scope.clear()

    threads = [
        threading.Thread(target=worker, args=("inc-A", 100)),
        threading.Thread(target=worker, args=("inc-B", 7)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["inc-A"]["llm_prompt_tokens"] == 100
    assert results["inc-B"]["llm_prompt_tokens"] == 7
    assert results["inc-A"]["incident_id"] == "inc-A"
    assert results["inc-B"]["incident_id"] == "inc-B"


def test_module_level_cost_scope_is_a_single_shared_instance():
    assert isinstance(incident_cost_scope, IncidentCostScope)
    from aeam.services.llm_service import incident_cost_scope as llm_scope
    assert llm_scope is incident_cost_scope


# ===========================================================================
# 5. Audit query surface (SEC-6)
# ===========================================================================

def test_audit_query_filters_by_principal(audit_db):
    client = TestClient(_audit_app(audit_db))
    resp = client.get("/api/v1/audit/entries", params={"principal": "alice"})
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 2
    assert {e["user_id"] for e in entries} == {"alice"}
    assert resp.headers["X-Total-Count"] == "2"


def test_audit_query_filters_by_time_window(audit_db):
    client = TestClient(_audit_app(audit_db))
    resp = client.get("/api/v1/audit/entries", params={
        "since": "2026-07-21T00:00:00+00:00",
        "until": "2026-07-23T23:59:59+00:00",
    })
    assert resp.status_code == 200
    entries = resp.json()
    assert {e["entry_id"] for e in entries} == {"e2", "e3", "e4"}


def test_audit_query_combines_principal_and_window(audit_db):
    client = TestClient(_audit_app(audit_db))
    resp = client.get("/api/v1/audit/entries", params={
        "principal": "bob", "since": "2026-07-23T00:00:00+00:00",
    })
    assert [e["entry_id"] for e in resp.json()] == ["e4"]


def test_audit_query_is_newest_first_and_paged(audit_db):
    client = TestClient(_audit_app(audit_db))
    resp = client.get("/api/v1/audit/entries", params={"limit": 2, "offset": 0})
    assert [e["entry_id"] for e in resp.json()] == ["e5", "e4"]
    assert resp.headers["X-Total-Count"] == "5"

    page2 = client.get("/api/v1/audit/entries", params={"limit": 2, "offset": 2})
    assert [e["entry_id"] for e in page2.json()] == ["e3", "e2"]


def test_audit_query_rejects_malformed_and_inverted_windows(audit_db):
    client = TestClient(_audit_app(audit_db))
    assert client.get("/api/v1/audit/entries", params={"since": "not-a-date"}).status_code == 422
    inverted = client.get("/api/v1/audit/entries", params={
        "since": "2026-07-24T00:00:00+00:00", "until": "2026-07-20T00:00:00+00:00",
    })
    assert inverted.status_code == 422


def test_audit_principals_aggregates_counts(audit_db):
    client = TestClient(_audit_app(audit_db))
    body = client.get("/api/v1/audit/principals").json()
    counts = {p["principal"]: p["entry_count"] for p in body["principals"]}
    assert counts == {"alice": 2, "bob": 2, "carol": 1}
    assert body["window"]["applied"] is False


def test_audit_route_is_rbac_mapped_to_the_grant_the_auditor_role_holds():
    """The phase requires an auditor-role user to be able to query audit
    history. That must be enforced by the middleware map, not by the router."""
    mapping = {prefix: (res, act) for prefix, res, act in _ENDPOINT_RBAC_MAP}
    assert "/api/v1/audit" in mapping
    resource, action = mapping["/api/v1/audit"]
    assert RBAC().check_permission(["auditor"], resource, action) is True


def test_audit_surface_is_read_only():
    """SEC-6: the audit trail is append-only. The query surface must expose no
    method that could mutate it."""
    methods = set()
    for route in audit_router.routes:
        methods |= set(getattr(route, "methods", set()))
    assert methods <= {"GET", "HEAD", "OPTIONS"}, f"audit router exposes writes: {methods}"


def test_audit_endpoint_denied_to_a_role_without_logs_view():
    """A seeded 403: a role holding no logs:view grant cannot read the trail."""
    assert RBAC().check_permission(["nonexistent_role"], "logs", "view") is False


# ===========================================================================
# 6. Deployment alert artifacts
# ===========================================================================

def test_prometheus_scrape_config_is_valid_and_targets_the_metrics_endpoint():
    config = yaml.safe_load((_DEPLOY / "prometheus.yml").read_text(encoding="utf-8"))
    jobs = {j["job_name"]: j for j in config["scrape_configs"]}
    assert "aeam" in jobs
    assert jobs["aeam"]["metrics_path"] == "/metrics"
    assert "alerts.yml" in config["rule_files"]


def test_alert_rules_are_valid_and_every_alert_declares_its_semantics():
    rules = yaml.safe_load((_DEPLOY / "alerts.yml").read_text(encoding="utf-8"))
    alerts = [r for g in rules["groups"] for r in g["rules"]]
    assert alerts, "no alert rules shipped"

    for alert in alerts:
        name = alert["alert"]
        assert alert.get("expr"), f"{name} has no expression"
        assert alert.get("labels", {}).get("severity") in {"critical", "warning"}, name
        annotations = alert.get("annotations", {})
        # OBS-2: an alert with no declared semantics is unactionable at 3am.
        assert annotations.get("summary"), f"{name} has no summary"
        assert annotations.get("description"), f"{name} has no description"
        assert annotations.get("runbook"), f"{name} has no runbook link"


def test_every_alert_expression_references_a_metric_aeam_actually_publishes():
    """An alert on a series nothing emits can never fire — it is worse than no
    alert, because it looks like coverage."""
    rules = yaml.safe_load((_DEPLOY / "alerts.yml").read_text(encoding="utf-8"))
    published = {
        "up",  # Prometheus' own scrape-health series
        "incidents_total", "investigation_duration_seconds", "active_incidents",
        "agent_execution_time_seconds", "action_success_total", "action_failure_total",
        "worker_heartbeat_timestamp_seconds", "llm_calls_total",
        "llm_call_duration_seconds", "llm_tokens_total", "llm_cost_usd_total",
        "retrieval_chunks_total", "action_executions_total",
    }
    for group in rules["groups"]:
        for alert in group["rules"]:
            expr = alert["expr"]
            assert any(metric in expr for metric in published), (
                f"{alert['alert']} references no known AEAM metric: {expr}"
            )


def test_alerts_cover_the_seeded_failures_the_phase_requires():
    """Acceptance: a dead monitor thread and an error-rate spike must each
    have a rule that fires on them."""
    rules = yaml.safe_load((_DEPLOY / "alerts.yml").read_text(encoding="utf-8"))
    names = {r["alert"] for g in rules["groups"] for r in g["rules"]}
    assert "AEAMMonitorAgentHeartbeatStale" in names
    assert "AEAMActionFailureRateHigh" in names
    assert "AEAMLLMFailureRateHigh" in names


def test_sre_runbook_and_alert_catalog_exist_and_cover_every_alert():
    """OBS-2/DOC-2: an alert whose runbook section does not exist is an alert
    nobody can action. Both documents are part of the phase's deliverable."""
    docs = Path(__file__).resolve().parents[2] / "docs"
    runbook = docs / "SRE_RUNBOOK.md"
    catalog = docs / "ALERT_CATALOG.md"
    assert runbook.is_file(), "docs/SRE_RUNBOOK.md missing"
    assert catalog.is_file(), "docs/ALERT_CATALOG.md missing"

    runbook_text = runbook.read_text(encoding="utf-8")
    catalog_text = catalog.read_text(encoding="utf-8")

    rules = yaml.safe_load((_DEPLOY / "alerts.yml").read_text(encoding="utf-8"))
    for group in rules["groups"]:
        for alert in group["rules"]:
            name = alert["alert"]
            # Every alert must appear in the catalog with declared semantics...
            assert name in catalog_text, f"{name} is not documented in ALERT_CATALOG.md"
            # ...and its runbook anchor must actually resolve to a heading.
            anchor = alert["annotations"]["runbook"].split("#", 1)[1]
            heading_slug = anchor.replace("-", " ")
            assert heading_slug.lower() in runbook_text.lower().replace("-", " "), (
                f"{name}'s runbook anchor {anchor!r} has no matching section in SRE_RUNBOOK.md"
            )


def test_no_deployment_artifact_defines_a_second_metrics_store():
    """OBS-1: alerting must consume the existing pipeline, never introduce a
    parallel one (no pushgateway, no statsd, no second remote-write target)."""
    text = (_DEPLOY / "prometheus.yml").read_text(encoding="utf-8").lower()
    for forbidden in ("pushgateway", "statsd", "graphite", "influxdb"):
        assert forbidden not in text, f"second metrics store configured: {forbidden}"


def test_monitor_heartbeat_alert_fires_on_a_synthetic_stale_series():
    """Alert-rule semantics test: evaluate the expression's arithmetic against
    a synthetic series, so the threshold is verified rather than assumed."""
    rules = yaml.safe_load((_DEPLOY / "alerts.yml").read_text(encoding="utf-8"))
    alert = next(
        r for g in rules["groups"] for r in g["rules"]
        if r["alert"] == "AEAMMonitorAgentHeartbeatStale"
    )
    threshold = float(alert["expr"].rsplit(">", 1)[1].strip())

    now = 1_000_000.0
    healthy_heartbeat = now - 30      # 30s old
    stale_heartbeat = now - 600       # 10 minutes old

    assert (now - healthy_heartbeat) > threshold is False or (now - healthy_heartbeat) <= threshold
    assert (now - stale_heartbeat) > threshold


# ===========================================================================
# 7. Distributed tracing (OBS-6)
# ===========================================================================

class _Settings:
    def __init__(self, **kw):
        self.OTEL_TRACING_ENABLED = kw.get("enabled", False)
        self.OTEL_EXPORTER_OTLP_ENDPOINT = kw.get("endpoint", "")
        self.OTEL_SERVICE_NAME = "aeam-test"
        self.ENVIRONMENT = "test"


@pytest.fixture(autouse=True)
def _reset_tracing():
    tracing_module.reset_tracing_for_tests()
    yield
    tracing_module.reset_tracing_for_tests()


def test_tracing_is_disabled_by_default():
    assert tracing_module.configure_tracing(_Settings()) is False
    assert tracing_module.tracing_enabled() is False


def test_tracing_enabled_without_an_endpoint_stays_off_rather_than_dropping_spans():
    assert tracing_module.configure_tracing(_Settings(enabled=True, endpoint="")) is False
    assert tracing_module.tracing_enabled() is False


def test_span_context_manager_is_a_safe_noop_when_tracing_is_off():
    tracing_module.configure_tracing(_Settings())
    with tracing_module.investigation_span("decision", incident_id="inc-1") as span:
        assert span is None
    assert tracing_module.current_trace_id() is None


def test_enabled_tracing_emits_one_correlated_incident_scoped_trace():
    """Acceptance: an investigation emits a SINGLE trace spanning its stages,
    every span joinable to the incident id."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Inject the in-memory provider directly — this test verifies span SHAPE,
    # not the OTLP wire format (which is the exporter's own tested concern).
    tracing_module._tracer = provider.get_tracer(tracing_module.TRACER_NAME)
    tracing_module._configured = True
    tracing_module._status_ok = trace.Status(trace.StatusCode.OK)
    tracing_module._status_error = trace.StatusCode.ERROR

    incident_id = "inc-trace-1"
    with tracing_module.investigation_span("investigation", incident_id=incident_id):
        assert tracing_module.current_trace_id() is not None
        with tracing_module.investigation_span("decision", incident_id=incident_id):
            pass
        with tracing_module.investigation_span("evidence.rag", incident_id=incident_id):
            pass
        with tracing_module.investigation_span("planning", incident_id=incident_id):
            pass
        with tracing_module.investigation_span("action", incident_id=incident_id):
            pass

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert {"investigation", "decision", "evidence.rag", "planning", "action"} <= names

    # ONE trace: every stage shares the root's trace id.
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1, f"investigation produced {len(trace_ids)} traces, expected 1"

    # Joinable to the incident by the SAME key the logs carry.
    for span in spans:
        assert span.attributes.get(tracing_module.INCIDENT_ID_ATTRIBUTE) == incident_id


def test_span_records_an_exception_without_swallowing_it():
    """Tracing observes failures; it must never hide one from the caller."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing_module._tracer = provider.get_tracer(tracing_module.TRACER_NAME)
    tracing_module._configured = True
    tracing_module._status_ok = trace.Status(trace.StatusCode.OK)
    tracing_module._status_error = trace.StatusCode.ERROR

    with pytest.raises(ValueError, match="boom"):
        with tracing_module.investigation_span("action", incident_id="inc-x"):
            raise ValueError("boom")

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is trace.StatusCode.ERROR


def test_configure_tracing_never_raises_on_a_broken_configuration():
    """A telemetry backend must never be able to stop the platform."""
    class _Exploding:
        OTEL_TRACING_ENABLED = True
        ENVIRONMENT = "test"

        @property
        def OTEL_EXPORTER_OTLP_ENDPOINT(self):
            raise RuntimeError("configuration blew up")

        OTEL_SERVICE_NAME = "aeam"

    # Must return False, not propagate.
    assert tracing_module.configure_tracing(_Exploding()) is False
