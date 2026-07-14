"""
aeam/api/data_center.py

Enterprise Data Center API — dataset activation and business/monitoring profile.

Complements ``aeam.api.knowledge`` (dataset catalog: list/detail/search/
status/preview/re-index/delete — all reused here unmodified, never
duplicated) with the two capabilities that genuinely did not exist anywhere
before this phase:

- **Activation** (activate/deactivate a dataset for autonomous monitoring).
  ``StaticDatasetActivation`` (B1.5.3) is frozen at bootstrap and has no
  mutation method — this is the anticipated extension point its own
  docstring reserved: :class:`~aeam.intelligence.dataset_activation.RedisDatasetActivation`,
  a second implementation of the same
  :class:`~aeam.intelligence.dataset_activation.DatasetActivation` protocol,
  wired in by ``main.py``. Neither ``CompositeKPISource`` nor
  ``CompositeRuleEngine`` change — they only ever call
  ``list_activated_dataset_ids()``.
- **Business/monitoring profile** — composes three already-existing,
  UNMODIFIED components with zero new computation logic of its own:
  :class:`~aeam.intelligence.dataset_intelligence.DatasetIntelligenceService`
  (measures/dimensions/identifiers/timestamp/forecast candidates/monitorable
  metrics), the live ``dataset_activation`` (is this dataset currently
  activated?), and a plain :class:`~aeam.agents.kpi.rule_engine.RuleEngine`
  instance (which of this dataset's metrics happen to match a curated,
  governed rule domain — "rule coverage" — vs. statistics-only monitoring).

Rules enforced (mirrors every other API module in this package):
- All state access via request.app.state.container.
- No ingestion, no BlobStore, no Qdrant calls, no new repository methods.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.intelligence.dataset_intelligence import DatasetIntelligenceError, DatasetIntelligenceService
from aeam.registry.repositories import DatasetRepository, SchemaRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/data-center", tags=["Data Center"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_dataset_or_404(container: Any, dataset_id: str):
    ds = DatasetRepository(container.db).get(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"No dataset with id {dataset_id!r}.")
    return ds


def _require_mutable_activation(container: Any):
    """
    Return ``container.dataset_activation`` if it supports ``activate``/
    ``deactivate`` (i.e. :class:`~aeam.intelligence.dataset_activation.RedisDatasetActivation`),
    else ``503`` — this endpoint never silently no-ops.
    """
    activation = getattr(container, "dataset_activation", None)
    if activation is None or not (hasattr(activation, "activate") and hasattr(activation, "deactivate")):
        raise HTTPException(
            status_code=503,
            detail="Dataset activation is not mutable in this deployment "
                   "(no RedisDatasetActivation configured).",
        )
    return activation


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

@router.get("/activation", summary="List currently activated dataset ids")
def get_activation(request: Request) -> JSONResponse:
    container = request.app.state.container
    activation = getattr(container, "dataset_activation", None)
    ids = activation.list_activated_dataset_ids() if activation is not None else []
    return JSONResponse(status_code=200, content={"activated_dataset_ids": ids})


@router.post("/datasets/{dataset_id}/activate", summary="Activate a dataset for monitoring")
def activate_dataset(request: Request, dataset_id: str) -> JSONResponse:
    """
    Mark ``dataset_id`` as activated. Takes effect on the next
    ``MonitorAgent`` cycle (``CompositeKPISource``/``CompositeRuleEngine``
    re-evaluate the activation list live, every cycle — no restart needed).
    """
    container = request.app.state.container
    _get_dataset_or_404(container, dataset_id)
    activation = _require_mutable_activation(container)
    activation.activate(dataset_id)
    logger.info("activate_dataset | dataset_id=%s", dataset_id)
    return JSONResponse(status_code=200, content={"dataset_id": dataset_id, "activated": True})


@router.post("/datasets/{dataset_id}/deactivate", summary="Deactivate a dataset from monitoring")
def deactivate_dataset(request: Request, dataset_id: str) -> JSONResponse:
    """Mark ``dataset_id`` as deactivated. See :func:`activate_dataset` for timing."""
    container = request.app.state.container
    _get_dataset_or_404(container, dataset_id)
    activation = _require_mutable_activation(container)
    activation.deactivate(dataset_id)
    logger.info("deactivate_dataset | dataset_id=%s", dataset_id)
    return JSONResponse(status_code=200, content={"dataset_id": dataset_id, "activated": False})


# ---------------------------------------------------------------------------
# Business / monitoring profile
# ---------------------------------------------------------------------------

@router.get("/datasets/{dataset_id}/profile", summary="Business and monitoring profile for a dataset")
def get_dataset_profile(request: Request, dataset_id: str) -> JSONResponse:
    """
    Compose the dataset's business profile (measures/dimensions/identifiers/
    timestamp/forecast candidates/monitorable metrics — from
    ``DatasetIntelligenceService.build_profile``, unmodified) with its current
    activation state and per-metric rule coverage.

    Always ``200`` for an existing dataset — a dataset not yet processed
    (no schema) reports ``available: false`` with the reason, matching the
    same "expected outcome, not a server error" convention as the Knowledge
    Center's preview endpoints.
    """
    container = request.app.state.container
    _get_dataset_or_404(container, dataset_id)

    intelligence = DatasetIntelligenceService(
        dataset_repo=DatasetRepository(container.db), schema_repo=SchemaRepository(container.db),
    )
    try:
        profile = intelligence.build_profile(dataset_id)
    except DatasetIntelligenceError as exc:
        return JSONResponse(status_code=200, content={
            "available": False, "reason": exc.reason, "detail": exc.detail,
        })

    activation = getattr(container, "dataset_activation", None)
    activated_ids = set(activation.list_activated_dataset_ids()) if activation is not None else set()
    activated = dataset_id in activated_ids

    # Curated (governed) rule domains — a fresh RuleEngine() is cheap and
    # side-effect-free (parses detection_rules.yaml); this is the SAME class
    # main.py itself constructs, never modified here.
    curated_domains = set(RuleEngine().loaded_domains)

    monitorable_metrics = [
        {
            "metric_id": m.metric_id,
            "column": m.column,
            "data_type": m.data_type,
            "dimensions": m.dimensions,
            "forecastable": m.forecastable,
            "rule_coverage": m.column in curated_domains,
        }
        for m in profile.monitorable_metrics
    ]

    return JSONResponse(status_code=200, content={
        "available": True,
        "dataset_id": dataset_id,
        "measures": profile.measures,
        "dimensions": profile.dimensions,
        "identifiers": profile.identifiers,
        "timestamp_column": profile.timestamp_column,
        "forecastable_metrics": profile.forecastable_metrics,
        "monitorable_metrics": monitorable_metrics,
        "activated": activated,
        "forecast_enabled": profile.timestamp_column is not None,
        "generated_at": profile.generated_at,
    })
