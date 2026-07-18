"""
aeam/tests/test_phase_d5_administration.py

Enterprise Administration & Settings UI (Phase D5) tests.

Exercises aeam/api/administration.py — the read/update/validate/reset
surface over the Phase D4 Enterprise Configuration Engine — against a real
FastAPI TestClient and a SCRATCH ``.env`` file (``administration._ENV_PATH``
is monkeypatched per-test so the real project ``.env`` is never touched).

Covers exactly the mission's stated behaviors:
- Read: every field carries default/configured/effective/restart_required.
- Update: atomic (no partial writes on validation failure), persists via
  the real Settings model's own Pydantic constraints.
- Validate: dry-run, never writes.
- Reset: restores defaults (removes the .env override), idempotent.
- Defaults are preserved when a value is unset (None means "use the
  engine's own hardcoded default" -- never a silently-different value).
- Nothing here ever touches incidents/findings -- there is no DatabaseClient
  or incidents import in aeam/api/administration.py at all.
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aeam.api.administration as admin_mod
from aeam.config.settings import Settings


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    # Settings.model_config declares extra="forbid", so this scratch file
    # must contain only recognised Settings fields (or be empty) -- a stray
    # unrecognised key would itself trigger a ValidationError unrelated to
    # whatever the test is actually exercising.
    path = tmp_path / ".env.test"
    path.write_text(
        "DATABASE_URL=sqlite:///test.db\n"
        "REDIS_URL=redis://localhost\n"
        "VECTOR_DB_URL=http://localhost:6333\n"
        "ENVIRONMENT=test\n"
    )
    monkeypatch.setattr(admin_mod, "_ENV_PATH", path)
    return path


@pytest.fixture()
def client(env_file):
    app = FastAPI()
    app.include_router(admin_mod.router)
    # No "effective" settings wired -- exercises the "container has no
    # settings yet" honest-degradation path.
    app.state.container = types.SimpleNamespace(settings=None)
    return TestClient(app)


@pytest.fixture()
def client_with_effective(env_file):
    """A container whose `.settings` mirrors what a real running app would
    have (fixed at 'last startup', all D4 fields at their None/unconfigured
    state) -- lets tests exercise restart_required meaningfully."""
    app = FastAPI()
    app.include_router(admin_mod.router)
    effective = Settings(
        DATABASE_URL="sqlite:///test.db", REDIS_URL="redis://localhost",
        VECTOR_DB_URL="http://localhost:6333", ENVIRONMENT="test",
    )
    app.state.container = types.SimpleNamespace(settings=effective)
    return TestClient(app), effective


# ===========================================================================
# 1. Read
# ===========================================================================

def test_get_returns_all_21_fields_grouped_into_8_sections(client):
    r = client.get("/api/v1/admin/config/")
    assert r.status_code == 200
    data = r.json()
    assert len(data["fields"]) == 21
    assert data["sections"] == [
        "Memory", "Policy", "Cross Dataset", "Adaptive Detection", "Retrieval",
        "Execution Planning", "AI Evaluation", "Observability",
    ]


def test_get_field_has_name_description_current_default(client):
    r = client.get("/api/v1/admin/config/")
    field = next(f for f in r.json()["fields"] if f["key"] == "POLICY_SIMILARITY_THRESHOLD")
    assert field["label"] == "Semantic match threshold"
    assert "PolicyRegistry" in field["description"]
    assert field["configured_value"] is None  # unset -- default preserved
    assert field["default"] == 0.4
    assert field["constraints"] == {"gt": 0.0, "le": 1.0}


def test_get_unset_field_preserves_default_and_is_not_overridden(client):
    r = client.get("/api/v1/admin/config/")
    field = next(f for f in r.json()["fields"] if f["key"] == "AI_EVAL_STRENGTH_THRESHOLD")
    assert field["is_overridden"] is False
    assert field["configured_value"] is None
    assert field["default"] == 0.7


def test_get_reports_default_note_for_non_numeric_defaults(client):
    r = client.get("/api/v1/admin/config/")
    field = next(f for f in r.json()["fields"] if f["key"] == "MEMORY_SIMILARITY_THRESHOLD")
    assert field["default"] is None
    assert "no extra filter" in field["default_note"].lower()


def test_get_reports_choices_for_human_approval_field(client):
    r = client.get("/api/v1/admin/config/")
    field = next(f for f in r.json()["fields"] if f["key"] == "HUMAN_APPROVAL_QUALITY_LEVELS")
    assert set(field["choices"]) == {"insufficient", "low", "medium", "high"}


def test_get_restart_required_when_configured_differs_from_effective(client_with_effective):
    client, _effective = client_with_effective
    client.put("/api/v1/admin/config/", json={"values": {"POLICY_SIMILARITY_THRESHOLD": 0.9}})
    r = client.get("/api/v1/admin/config/")
    field = next(f for f in r.json()["fields"] if f["key"] == "POLICY_SIMILARITY_THRESHOLD")
    assert field["configured_value"] == 0.9
    assert field["effective_value"] is None  # the running process was never restarted
    assert field["restart_required"] is True
    assert r.json()["restart_required"] is True


# ===========================================================================
# 2. Validate (dry-run, never writes)
# ===========================================================================

def test_validate_rejects_out_of_bounds_value(client, env_file):
    before = env_file.read_text()
    r = client.post("/api/v1/admin/config/validate", json={"values": {"POLICY_SIMILARITY_THRESHOLD": 5.0}})
    assert r.status_code == 200
    body = r.json()
    assert body["all_valid"] is False
    assert body["results"]["POLICY_SIMILARITY_THRESHOLD"]["valid"] is False
    assert env_file.read_text() == before  # nothing written


def test_validate_accepts_in_bounds_value(client):
    r = client.post("/api/v1/admin/config/validate", json={"values": {"POLICY_SIMILARITY_THRESHOLD": 0.6}})
    body = r.json()
    assert body["all_valid"] is True
    assert body["results"]["POLICY_SIMILARITY_THRESHOLD"] == {"valid": True, "error": None}


def test_validate_rejects_unknown_field(client):
    r = client.post("/api/v1/admin/config/validate", json={"values": {"NOT_A_REAL_FIELD": 1}})
    body = r.json()
    assert body["all_valid"] is False
    assert "not a recognised" in body["results"]["NOT_A_REAL_FIELD"]["error"].lower()


def test_validate_requires_nonempty_values(client):
    r = client.post("/api/v1/admin/config/validate", json={"values": {}})
    assert r.status_code == 422


# ===========================================================================
# 3. Update (atomic; persists to .env)
# ===========================================================================

def test_update_persists_valid_value(client, env_file):
    r = client.put("/api/v1/admin/config/", json={"values": {"OBSERVABILITY_TREND_WINDOW": 7}})
    assert r.status_code == 200
    field = next(f for f in r.json()["fields"] if f["key"] == "OBSERVABILITY_TREND_WINDOW")
    assert field["configured_value"] == 7
    assert "OBSERVABILITY_TREND_WINDOW" in env_file.read_text()


def test_update_is_atomic_no_partial_write_on_invalid_field(client, env_file):
    before = env_file.read_text()
    r = client.put("/api/v1/admin/config/", json={
        "values": {"POLICY_SIMILARITY_THRESHOLD": 0.6, "AI_EVAL_STRENGTH_THRESHOLD": 5.0},
    })
    assert r.status_code == 422
    assert "AI_EVAL_STRENGTH_THRESHOLD" in r.json()["detail"]["errors"]
    # Neither field was written -- not even the valid one.
    assert env_file.read_text() == before


def test_update_null_value_is_equivalent_to_reset(client, env_file):
    client.put("/api/v1/admin/config/", json={"values": {"OBSERVABILITY_TREND_WINDOW": 7}})
    assert "OBSERVABILITY_TREND_WINDOW" in env_file.read_text()
    r = client.put("/api/v1/admin/config/", json={"values": {"OBSERVABILITY_TREND_WINDOW": None}})
    assert r.status_code == 200
    field = next(f for f in r.json()["fields"] if f["key"] == "OBSERVABILITY_TREND_WINDOW")
    assert field["configured_value"] is None
    assert field["default"] == 20


def test_update_requires_nonempty_values(client):
    r = client.put("/api/v1/admin/config/", json={"values": {}})
    assert r.status_code == 422


# ===========================================================================
# 4. Reset
# ===========================================================================

def test_reset_one_field_restores_default(client, env_file):
    client.put("/api/v1/admin/config/", json={"values": {"POLICY_SIMILARITY_THRESHOLD": 0.9}})
    r = client.post("/api/v1/admin/config/reset", json={"keys": ["POLICY_SIMILARITY_THRESHOLD"]})
    assert r.status_code == 200
    field = next(f for f in r.json()["fields"] if f["key"] == "POLICY_SIMILARITY_THRESHOLD")
    assert field["configured_value"] is None
    assert "POLICY_SIMILARITY_THRESHOLD" not in env_file.read_text()


def test_reset_is_idempotent_on_already_default_field(client):
    r = client.post("/api/v1/admin/config/reset", json={"keys": ["POLICY_SIMILARITY_THRESHOLD"]})
    assert r.status_code == 200  # never errors just because it was already at default


def test_reset_all_restores_every_field(client, env_file):
    client.put("/api/v1/admin/config/", json={
        "values": {"POLICY_SIMILARITY_THRESHOLD": 0.9, "OBSERVABILITY_TREND_WINDOW": 5},
    })
    r = client.post("/api/v1/admin/config/reset", json={"all": True})
    assert r.status_code == 200
    data = r.json()
    assert all(f["configured_value"] is None for f in data["fields"])
    assert data["restart_required"] is False  # nothing configured means nothing to reconcile


def test_reset_rejects_unknown_field(client):
    r = client.post("/api/v1/admin/config/reset", json={"keys": ["NOT_A_REAL_FIELD"]})
    assert r.status_code == 422


# ===========================================================================
# 5. Never touches historical investigation data
# ===========================================================================

def test_administration_module_has_no_database_or_incidents_dependency():
    """Structural guarantee: this module cannot alter historical
    investigations because it never imports anything that could reach the
    incidents table."""
    import inspect
    source = inspect.getsource(admin_mod)
    assert "DatabaseClient" not in source
    assert "incidents" not in source.lower()
