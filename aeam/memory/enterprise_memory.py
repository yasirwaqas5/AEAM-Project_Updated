"""
aeam/memory/enterprise_memory.py

Enterprise Memory Engine (Phase C1) — organizational memory over resolved
incidents.

Turns every finalized incident into a reusable evidence source for future
investigations, using the SAME embedding model, Qdrant deployment, and
ingestion/retrieval pipeline classes the RAG document pipeline already uses
(:class:`~aeam.agents.rag.ingestion_pipeline.IngestionPipeline` /
:class:`~aeam.agents.rag.retrieval_pipeline.RetrievalPipeline`) — pointed at
a second, dedicated Qdrant collection (``"aeam_incident_memories"``) rather
than a second vector store or a second embedding model. This is composition,
not duplication: Qdrant already supports multiple named collections, and
both pipeline classes are already collection-parametrized (see their
``collection`` constructor argument). Neither class is modified here.

This module makes no LLM calls, no decisions, and never talks to the
Orchestrator directly — the Orchestrator calls into it explicitly (see
``Orchestrator.investigate`` / ``Orchestrator.finalize_incident``), exactly
the same way it already calls into RAGAgent/ActionAgent/ReportAgent.

Never fabricates a result: if no similar resolved incident exists, or a
field genuinely was not recorded for an incident (e.g. no root cause was
ever determined), the caller gets an empty list / an absent key — never an
invented similarity score, an invented historical incident, or a guessed
field value.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aeam.agents.rag.ingestion_pipeline import IngestionPipeline
from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline

logger = logging.getLogger(__name__)

#: Default number of similar incidents returned per investigation.
DEFAULT_TOP_K: int = 3

#: Fields the Memory Contents specification asks for that this component
#: never populates, and why: "human_modifications" would require reaching
#: into the Human Review verdict-recording path, which runs asynchronously
#: AFTER an incident is finalized — a second, independent integration point
#: outside the investigation flow this phase extends. Rather than fabricate
#: or guess at it, this field is simply never written.


class EnterpriseMemoryEngine:
    """
    Organizational memory over resolved incidents, built entirely from
    existing RAG plumbing pointed at a second Qdrant collection.

    Args:
        ingestion_pipeline: An :class:`IngestionPipeline` instance already
                            configured with the memory collection (e.g.
                            ``collection="aeam_incident_memories"``). The
                            SAME class used for document ingestion — not a
                            new writer.
        retrieval_pipeline: A :class:`RetrievalPipeline` instance already
                            configured with the same memory collection. The
                            SAME class used for document retrieval — not a
                            new searcher.
        top_k:              Default number of similar incidents to return
                            when the caller doesn't override it.

    Raises:
        ValueError: If either pipeline is ``None``.
    """

    def __init__(
        self,
        ingestion_pipeline: IngestionPipeline,
        retrieval_pipeline: RetrievalPipeline,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        if ingestion_pipeline is None:
            raise ValueError("ingestion_pipeline must not be None.")
        if retrieval_pipeline is None:
            raise ValueError("retrieval_pipeline must not be None.")

        self._ingest: IngestionPipeline = ingestion_pipeline
        self._retrieve: RetrievalPipeline = retrieval_pipeline
        self._top_k: int = max(1, int(top_k))

    # ------------------------------------------------------------------
    # Write path — called from Orchestrator.finalize_incident()
    # ------------------------------------------------------------------

    def remember_incident(self, incident: dict[str, Any]) -> dict[str, Any] | None:
        """
        Embed and store a resolved incident as a reusable memory.

        ``incident`` is expected to carry the same kind of fields the
        Orchestrator already assembles for
        :meth:`~aeam.memory.long_term.LongTermMemory.record_incident`, plus a
        few fields it computes locally but doesn't otherwise persist
        (``investigation_status``, ``recommended_actions``,
        ``executed_actions``, ``chunk_ids``). Every field besides
        ``incident_id`` is optional — whatever the caller doesn't supply is
        simply omitted from the stored memory, never invented.

        Args:
            incident: Dict of incident fields. Recognised keys: ``incident_id``
                (required), ``event_type``, ``metric``, ``severity``,
                ``root_cause``, ``confidence``, ``investigation_status``,
                ``recommended_actions``, ``executed_actions``, ``chunk_ids``,
                ``timestamp``.

        Returns:
            The underlying :meth:`IngestionPipeline.ingest_document` result
            dict, or ``None`` if ``incident_id`` is missing (nothing to key
            the memory on) or storage failed (logged, never raised — a
            memory-write failure must never break incident finalization).
        """
        incident_id = incident.get("incident_id")
        if not incident_id:
            logger.warning("remember_incident | no incident_id supplied — skipping.")
            return None

        event_type = incident.get("event_type")
        metric = incident.get("metric")
        severity = incident.get("severity")
        root_cause = incident.get("root_cause")
        resolution_status = incident.get("investigation_status")
        confidence = incident.get("confidence")
        recommended_actions = incident.get("recommended_actions") or []
        executed_actions = incident.get("executed_actions") or []
        chunk_ids = incident.get("chunk_ids") or []
        timestamp = incident.get("timestamp")

        incident_summary = _build_incident_summary(event_type, metric, severity)
        investigation_summary = root_cause or "No root cause was determined during this investigation."

        text_parts = [incident_summary, investigation_summary]
        if recommended_actions:
            text_parts.append("Recommended actions: " + "; ".join(recommended_actions))
        text = " ".join(part for part in text_parts if part)

        # `date` is a required IngestionPipeline field (same contract as
        # document ingestion) — fall back to the current time only for this
        # generic technical requirement when the incident genuinely has no
        # timestamp. The incident's OWN timestamp (below) is only ever set
        # in the payload when it's genuinely known.
        ingestion_date = timestamp or datetime.now(tz=timezone.utc).isoformat()

        metadata: dict[str, Any] = {
            "source": incident_id,
            "date": ingestion_date,
            "doc_type": "incident_memory",
            "incident_id": incident_id,
            "incident_summary": incident_summary,
            "investigation_summary": investigation_summary,
        }
        # Only ever add a field when it genuinely exists — no fabricated
        # defaults for category/severity/root_cause/etc.
        optional_fields: dict[str, Any] = {
            "category": event_type,
            "severity": severity,
            "triggered_metric": metric,
            "root_cause": root_cause,
            "confidence": confidence,
            "resolution_status": resolution_status,
            "recommended_actions": recommended_actions or None,
            "executed_actions": executed_actions or None,
            "evidence_chunk_ids": chunk_ids or None,
            "timestamp": timestamp,
        }
        for key, value in optional_fields.items():
            if value is not None:
                metadata[key] = value

        try:
            result = self._ingest.ingest_document(text=text, metadata=metadata)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "remember_incident | failed to store memory | incident_id=%s | error=%s",
                incident_id, exc,
            )
            return None

        logger.info(
            "remember_incident | stored | incident_id=%s | collection=%s | chunks=%d",
            incident_id, result.get("collection"), result.get("chunks_upserted", 0),
        )
        return result

    # ------------------------------------------------------------------
    # Read path — called from Orchestrator.investigate()
    # ------------------------------------------------------------------

    def recall_similar_incidents(
        self,
        query: str,
        top_k: int | None = None,
        exclude_incident_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search Enterprise Memory for resolved incidents similar to ``query``.

        Reuses :meth:`RetrievalPipeline.search` exactly as RAGAgent does for
        document chunks — same embedding call, same Qdrant query, same
        similarity threshold — just against the memory collection.

        Args:
            query:               Natural-language query. Reuse the SAME
                                  query text the current investigation's RAG
                                  pass uses (e.g. via
                                  ``RAGAgent._formulate_query(event)``) so
                                  memory and document retrieval search on
                                  identical vocabulary.
            top_k:                Max matches to return. Defaults to the
                                  value passed to ``__init__``.
            exclude_incident_id: Skip this incident_id if present among the
                                  results (e.g. to avoid a live incident
                                  "matching" itself if it were ever recalled
                                  mid-investigation).

        Returns:
            List of match dicts, each with only the fields that were
            genuinely present when the memory was stored::

                {
                    "incident_id": str,
                    "similarity": float,
                    "category": str | None,
                    "severity": str | None,
                    "triggered_metric": str | None,
                    "root_cause": str | None,
                    "resolution_status": str | None,
                    "confidence": float | None,
                    "timestamp": str | None,
                    "incident_summary": str | None,
                }

            Empty list if nothing meets the retrieval pipeline's similarity
            threshold, if ``query`` is blank, or on any retrieval error
            (logged, never raised — mirrors RAGAgent's own
            never-break-the-investigation-loop error handling).
        """
        if not query or not query.strip():
            return []

        k = max(1, int(top_k or self._top_k))
        # Over-fetch by one extra slot when excluding an id, so a genuine
        # exclusion never silently shrinks the result count below k.
        fetch_k = k + 1 if exclude_incident_id else k

        try:
            hits = self._retrieve.search(query=query.strip(), top_k=fetch_k)
        except Exception as exc:  # noqa: BLE001
            logger.error("recall_similar_incidents | search failed | error=%s", exc)
            return []

        matches: list[dict[str, Any]] = []
        for hit in hits:
            meta = hit.get("metadata") or {}
            incident_id = meta.get("incident_id")
            if exclude_incident_id and incident_id == exclude_incident_id:
                continue
            matches.append({
                "incident_id": incident_id,
                "similarity": hit.get("similarity"),
                "category": meta.get("category"),
                "severity": meta.get("severity"),
                "triggered_metric": meta.get("triggered_metric"),
                "root_cause": meta.get("root_cause"),
                "resolution_status": meta.get("resolution_status"),
                "confidence": meta.get("confidence"),
                "timestamp": meta.get("timestamp"),
                "incident_summary": meta.get("incident_summary"),
            })
            if len(matches) >= k:
                break

        return matches

    @property
    def collection(self) -> str:
        """The Qdrant collection backing Enterprise Memory."""
        return self._retrieve.collection

    def __repr__(self) -> str:
        return f"EnterpriseMemoryEngine(collection={self._retrieve.collection!r}, top_k={self._top_k})"


def _build_incident_summary(event_type: str | None, metric: str | None, severity: str | None) -> str:
    """
    Compose a short, honest natural-language incident description from
    whichever of event_type/metric/severity are actually present — never
    invents a category or metric that wasn't recorded.
    """
    if not event_type and not metric:
        return "Incident (event type and metric unavailable)."

    desc = (event_type or "incident").replace("_", " ").lower()
    parts = [desc[:1].upper() + desc[1:]]
    if metric:
        parts.append(f"on '{metric}'")
    if severity:
        parts.append(f"(severity {severity})")
    return " ".join(parts) + "."
