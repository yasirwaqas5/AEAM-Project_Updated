"""
aeam/api/retrieval_debug.py

Developer-only retrieval debug/explainability API for the AEAM RAG system.

Exposes a single read-only endpoint that traces the retrieval pipeline
(query expansion, dense search, BM25 search, RRF fusion, cross-encoder
reranking, evidence diversity) for a free-text query — for developers
diagnosing retrieval quality. Never called by RAGAgent, the Orchestrator, or
any production code path; this is observability tooling only.

Rules enforced:
- All state access via request.app.state.container.
- Developer-only: returns 404 (not 403 — existence is not disclosed) when
  ``ENVIRONMENT == "production"``.
- Read-only; runs retrieval only, never writes to any database.
- Does not modify the retrieval pipeline or any of its components.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/debug/retrieval", tags=["Debug"])


@router.get(
    "/",
    summary="Trace the retrieval pipeline for a query (developer-only)",
    response_description="Stage-by-stage retrieval trace with provenance and timing.",
)
def debug_retrieval(
    request: Request,
    query: str = Query(..., min_length=1, description="Free-text query to trace."),
    top_k: int = Query(5, ge=1, le=50, description="Final number of chunks to return."),
    metadata: str | None = Query(
        None,
        description=(
            "Phase C6: optional JSON-encoded incident metadata dict (e.g. "
            '\'{"service": "checkout"}\') enabling the entity-extraction and '
            "metadata-aware-filtering stages for this trace. AEAM does not "
            "persist event.metadata on the incident record, so a historical "
            "incident's original metadata cannot be auto-supplied — pass it "
            "explicitly to exercise these stages. Invalid/absent JSON is "
            "silently treated as no metadata (never a 500)."
        ),
    ),
) -> JSONResponse:
    """
    Trace retrieval for ``query`` through every pipeline stage.

    See :class:`~aeam.agents.rag.retrieval_debug.RetrievalDebugTracer` for the
    full response schema. Disabled (404) outside development/staging to avoid
    exposing internal retrieval mechanics or corpus contents in production.

    Args:
        request:  Incoming FastAPI request. Used to access the app container.
        query:    Free-text query to trace (query parameter).
        top_k:    Final number of chunks to return (query parameter, default 5).
        metadata: Optional JSON-encoded incident metadata (Phase C6; query
                  parameter). Enables entity extraction / metadata-aware
                  filtering stages when provided.

    Returns:
        ``200`` — Full trace JSON.
        ``404`` — Not found (production environment — endpoint hidden).
        ``422`` — Invalid input (empty query, invalid top_k).
        ``503`` — Retrieval debug tracer not available (RAG not initialised).
        ``500`` — Unexpected trace failure.
    """
    container = request.app.state.container

    if str(getattr(container.settings, "ENVIRONMENT", "")).lower() == "production":
        # Deliberately 404, not 403 — do not disclose this endpoint exists.
        raise HTTPException(status_code=404, detail="Not found.")

    tracer = getattr(container, "rag_debug_tracer", None)
    if tracer is None:
        raise HTTPException(
            status_code=503,
            detail="Retrieval debug tracer is not available (RAG not initialised).",
        )

    parsed_metadata: dict[str, Any] | None = None
    if metadata:
        try:
            candidate = json.loads(metadata)
            if isinstance(candidate, dict):
                parsed_metadata = candidate
            else:
                logger.warning(
                    "debug_retrieval | metadata param was not a JSON object — ignoring."
                )
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "debug_retrieval | metadata param was not valid JSON — ignoring | raw=%r",
                metadata,
            )

    try:
        trace: dict[str, Any] = tracer.trace(query=query, top_k=top_k, metadata=parsed_metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("debug_retrieval | trace failed | query=%r | error=%s", query, exc)
        raise HTTPException(status_code=500, detail=f"Retrieval trace failed: {exc}") from exc

    logger.info(
        "debug_retrieval | query=%r | top_k=%d | final_chunks=%d | total_ms=%.1f",
        query, top_k, len(trace.get("final_chunks", [])),
        trace.get("timings_ms", {}).get("total_retrieval_latency_ms", 0.0),
    )
    return JSONResponse(status_code=200, content=trace)
