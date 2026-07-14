"""
aeam/tests/test_phase_agent_observatory.py

Phase: Agent Observatory — backend addition.

Exercises the one new, minimal endpoint this phase added:
GET /api/v1/system/rule-engine. It constructs a fresh, unmodified
RuleEngine() and returns its already-computed loaded_domains — no container,
no database, no Qdrant, no Redis required.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.api.system import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_rule_engine_endpoint_matches_real_ruleengine():
    client = _client()
    r = client.get("/api/v1/system/rule-engine")
    assert r.status_code == 200
    body = r.json()

    expected = RuleEngine().loaded_domains
    assert body["loaded_domains"] == expected
    assert body["count"] == len(expected)


def test_rule_engine_endpoint_returns_curated_domains():
    client = _client()
    body = client.get("/api/v1/system/rule-engine").json()
    # The curated domains this repository's detection_rules.yaml defines.
    assert set(body["loaded_domains"]) >= {"sales", "complaints", "inventory"}


def test_rule_engine_endpoint_never_touches_container():
    """No app.state.container is attached — the endpoint must not require one."""
    client = _client()
    r = client.get("/api/v1/system/rule-engine")
    assert r.status_code == 200
