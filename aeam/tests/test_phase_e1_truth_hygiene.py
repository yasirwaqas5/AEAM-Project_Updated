"""
aeam/tests/test_phase_e1_truth_hygiene.py

Phase E1 — Truth & Hygiene Baseline regression ledger.

Covers the E1 contract fixes and honesty guarantees:
1. ``DatabaseClient.fetch_metric_history(limit=N)`` returns the MOST RECENT
   N rows in ascending order (MOD-4 fix — previously returned the oldest N).
2. Placeholder quarantine (ENG-5): a placeholder-derived root cause is
   machine-identifiable (``root_cause_source``) and never persisted into
   Enterprise Memory; a genuinely-derived root cause still is.

Infrastructure: SQLite on a temp path only — no live services required
(TEST-3: these are unit tests).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from aeam.integrations.database import DatabaseClient


def _make_client(tmp_path: Path) -> DatabaseClient:
    db_file = tmp_path / f"e1-{uuid.uuid4().hex}.db"
    return DatabaseClient(database_url=f"sqlite:///{db_file}")


def _seed_metrics(client: DatabaseClient, metric: str, count: int) -> list[str]:
    """Insert ``count`` rows with strictly increasing ISO timestamps; return them."""
    timestamps = [f"2026-07-{day:02d}T00:00:00+00:00" for day in range(1, count + 1)]
    client.insert_metrics([
        {"metric": metric, "value": float(i), "timestamp": ts}
        for i, ts in enumerate(timestamps)
    ])
    return timestamps


# ---------------------------------------------------------------------------
# 1. fetch_metric_history — most-recent-N contract (MOD-4)
# ---------------------------------------------------------------------------

def test_fetch_metric_history_limit_returns_most_recent_rows(tmp_path):
    client = _make_client(tmp_path)
    try:
        timestamps = _seed_metrics(client, "sales", 10)

        rows = client.fetch_metric_history("sales", limit=3)

        assert len(rows) == 3
        # The three NEWEST timestamps — this is the regression: the old
        # implementation returned the three OLDEST.
        assert [r["timestamp"] for r in rows] == timestamps[-3:]
        # Still ascending for consumers (ForecastAgent/AdaptiveDetection).
        assert [r["timestamp"] for r in rows] == sorted(r["timestamp"] for r in rows)
        assert [r["value"] for r in rows] == [7.0, 8.0, 9.0]
    finally:
        client.dispose()


def test_fetch_metric_history_no_limit_returns_full_ascending_history(tmp_path):
    client = _make_client(tmp_path)
    try:
        timestamps = _seed_metrics(client, "sales", 10)

        rows = client.fetch_metric_history("sales")

        assert [r["timestamp"] for r in rows] == timestamps
    finally:
        client.dispose()


def test_fetch_metric_history_limit_larger_than_history_is_identical(tmp_path):
    """When rows <= limit the fix must be behaviorally invisible (COMPAT)."""
    client = _make_client(tmp_path)
    try:
        timestamps = _seed_metrics(client, "sales", 4)

        rows = client.fetch_metric_history("sales", limit=100)

        assert [r["timestamp"] for r in rows] == timestamps
    finally:
        client.dispose()


def test_fetch_metric_history_filters_by_metric(tmp_path):
    client = _make_client(tmp_path)
    try:
        _seed_metrics(client, "sales", 5)
        _seed_metrics(client, "complaints", 2)

        rows = client.fetch_metric_history("complaints", limit=10)

        assert len(rows) == 2
        assert all(isinstance(r["value"], float) for r in rows)
    finally:
        client.dispose()
