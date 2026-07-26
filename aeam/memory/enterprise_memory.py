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
        similarity_threshold: Optional additional similarity floor (Phase
                            D4 Enterprise Configuration Engine), applied ON
                            TOP OF whatever ``retrieval_pipeline.search()``
                            already returns -- it never reaches into or
                            modifies the injected ``RetrievalPipeline``
                            instance itself (which may be shared with
                            document RAG retrieval), so this only ever
                            makes Enterprise Memory recall STRICTER, never
                            looser, and never affects document retrieval.
                            ``None`` (the default) applies no extra filter.

    Raises:
        ValueError: If either pipeline is ``None``.
    """

    def __init__(
        self,
        ingestion_pipeline: IngestionPipeline,
        retrieval_pipeline: RetrievalPipeline,
        top_k: int = DEFAULT_TOP_K,
        similarity_threshold: float | None = None,
    ) -> None:
        if ingestion_pipeline is None:
            raise ValueError("ingestion_pipeline must not be None.")
        if retrieval_pipeline is None:
            raise ValueError("retrieval_pipeline must not be None.")

        self._ingest: IngestionPipeline = ingestion_pipeline
        self._retrieve: RetrievalPipeline = retrieval_pipeline
        self._top_k: int = max(1, int(top_k))
        self._similarity_threshold: float | None = similarity_threshold

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

        # Phase E12 (MEM-4): when this write is a CORRECTION rewrite, carry the
        # correction provenance into the stored memory so a future
        # investigation that recalls it can see it was corrected, by whom, and
        # why — a corrected memory that looks identical to an original one
        # would hide exactly the fact an auditor needs.
        provenance = incident.get("_correction_provenance")
        if isinstance(provenance, dict) and provenance:
            for key, value in provenance.items():
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

        if self._similarity_threshold is not None:
            hits = [h for h in hits if (h.get("similarity") or 0.0) >= self._similarity_threshold]

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

    # ------------------------------------------------------------------
    # Curation path — Phase E12 (MEM-4)
    # ------------------------------------------------------------------

    def expunge_incident_memory(
        self,
        incident_id: str,
        *,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        """
        Permanently remove one incident's memory from recall (MEM-4).

        Before this phase organizational memory had no correction path: a
        memory recorded from a wrong root cause kept surfacing as evidence in
        every future investigation, with no way to withdraw it short of
        deleting Qdrant points by hand. MEM-4 requires a correction path;
        this is the destructive half of it.

        Uses the SAME Qdrant client the ingestion pipeline already holds
        (``IngestionPipeline._qdrant``) with a payload filter on
        ``incident_id`` — the field :meth:`remember_incident` already writes
        into every point's payload. No new client, no new collection, no
        second delete mechanism.

        ``reason`` and ``actor`` are REQUIRED, not optional: an unattributed,
        unexplained deletion from an audited store is exactly the thing MEM-4
        exists to prevent. The caller (the curation API) writes the
        tamper-evident audit record; this method returns the facts that
        record needs and refuses to act without them.

        Args:
            incident_id: The incident whose memory should be withdrawn.
            reason:      Why it is being withdrawn. Recorded verbatim.
            actor:       The acting principal.

        Returns:
            ``{"expunged": bool, "incident_id": str, "points_deleted": int|None,
            "reason": str, "actor": str, "expunged_at": str}``.
            ``points_deleted`` is ``None`` when Qdrant does not report a
            count — reported honestly as unknown rather than guessed.

        Raises:
            ValueError: If ``incident_id``, ``reason``, or ``actor`` is blank.
            RuntimeError: If the delete fails. Unlike the write path (where a
                          failure must never break finalization), a curation
                          failure MUST surface: silently reporting success
                          would leave a withdrawn memory still recallable.
        """
        incident_id, reason, actor = self._require_curation_args(incident_id, reason, actor)

        try:
            from qdrant_client.http import models as qmodels

            result = self._ingest._qdrant.delete(
                collection_name=self._ingest.collection,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="incident_id",
                                match=qmodels.MatchValue(value=incident_id),
                            )
                        ]
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "expunge_incident_memory | FAILED | incident_id=%s | actor=%s | error=%s",
                incident_id, actor, exc,
            )
            raise RuntimeError(
                f"Failed to expunge memory for incident {incident_id!r}: {exc}"
            ) from exc

        record = {
            "expunged": True,
            "incident_id": incident_id,
            # Qdrant's delete-by-filter acknowledges the operation but does
            # not report how many points it removed. `None` says "not
            # reported"; inventing a count would be a fabricated fact in an
            # audited record.
            "points_deleted": None,
            "operation_id": getattr(result, "operation_id", None),
            "reason": reason,
            "actor": actor,
            "expunged_at": datetime.now(tz=timezone.utc).isoformat(),
            "collection": self._ingest.collection,
        }
        logger.info(
            "expunge_incident_memory | SUCCESS | incident_id=%s | actor=%s | reason=%s",
            incident_id, actor, reason,
        )
        return record

    def correct_incident_memory(
        self,
        incident_id: str,
        corrections: dict[str, Any],
        *,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        """
        Replace one incident's memory with a corrected version (MEM-4).

        The non-destructive half of the correction path. Implemented as
        expunge-then-rewrite rather than an in-place payload patch, because
        the memory's EMBEDDING is derived from the same text as its metadata:
        patching ``root_cause`` in the payload without re-embedding would
        leave a memory that recalls on its old, wrong text while displaying
        its corrected one — a subtler dishonesty than not correcting at all.

        The rewritten memory carries the correction provenance in its own
        metadata (``corrected``/``corrected_by``/``corrected_at``/
        ``correction_reason``), so a future investigation that recalls it can
        see it was corrected, by whom, and why.

        Args:
            incident_id: The incident whose memory is being corrected.
            corrections: Field overrides applied on top of the existing
                         memory, e.g. ``{"root_cause": "…"}``. Only the
                         recognised :meth:`remember_incident` keys are used;
                         anything else is ignored rather than silently
                         written into a shape nothing reads.
            reason:      Why the correction is being made. Recorded verbatim.
            actor:       The acting principal.

        Returns:
            ``{"corrected": True, "incident_id": …, "fields_corrected": [...],
            "previous": {...}, "reason": …, "actor": …, "corrected_at": …}``.

        Raises:
            ValueError:   If required arguments are blank, or ``corrections``
                          is empty (a correction that corrects nothing is a
                          caller bug, not a no-op to swallow).
            LookupError:  If no memory exists for ``incident_id`` — there is
                          nothing to correct, and creating one here would
                          fabricate organizational memory.
            RuntimeError: If the rewrite fails.
        """
        incident_id, reason, actor = self._require_curation_args(incident_id, reason, actor)
        if not corrections:
            raise ValueError("corrections must not be empty.")

        existing = self._find_memory_payload(incident_id)
        if existing is None:
            raise LookupError(
                f"No Enterprise Memory entry exists for incident {incident_id!r}; "
                "nothing to correct."
            )

        recognised = {
            "event_type", "metric", "severity", "root_cause", "confidence",
            "investigation_status", "recommended_actions", "executed_actions",
            "chunk_ids", "timestamp",
        }
        applied = {k: v for k, v in corrections.items() if k in recognised}
        if not applied:
            raise ValueError(
                f"none of {sorted(corrections)} is a correctable memory field; "
                f"correctable fields are {sorted(recognised)}."
            )

        # Rebuild the full incident dict from what was stored, then overlay
        # the corrections — so a correction to one field never silently drops
        # the others.
        rebuilt: dict[str, Any] = {
            "incident_id": incident_id,
            "event_type": existing.get("category"),
            "metric": existing.get("triggered_metric"),
            "severity": existing.get("severity"),
            "root_cause": existing.get("root_cause"),
            "confidence": existing.get("confidence"),
            "investigation_status": existing.get("resolution_status"),
            "recommended_actions": existing.get("recommended_actions") or [],
            "executed_actions": existing.get("executed_actions") or [],
            "chunk_ids": existing.get("evidence_chunk_ids") or [],
            "timestamp": existing.get("timestamp"),
        }
        previous = {field: rebuilt.get(field) for field in applied}
        rebuilt.update(applied)

        corrected_at = datetime.now(tz=timezone.utc).isoformat()

        # Withdraw the stale vector first so a failure mid-way can never leave
        # BOTH versions recallable (which would be worse than either alone).
        self.expunge_incident_memory(
            incident_id, reason=f"superseded by correction: {reason}", actor=actor,
        )

        result = self.remember_incident({
            **rebuilt,
            "_correction_provenance": {
                "corrected": True,
                "corrected_by": actor,
                "corrected_at": corrected_at,
                "correction_reason": reason,
                "fields_corrected": sorted(applied),
            },
        })
        if result is None:
            raise RuntimeError(
                f"Failed to rewrite corrected memory for incident {incident_id!r}. "
                "The stale memory was already withdrawn, so it no longer recalls; "
                "re-run the correction to restore a corrected entry."
            )

        record = {
            "corrected": True,
            "incident_id": incident_id,
            "fields_corrected": sorted(applied),
            "previous": previous,
            "reason": reason,
            "actor": actor,
            "corrected_at": corrected_at,
            "collection": self._ingest.collection,
        }
        logger.info(
            "correct_incident_memory | SUCCESS | incident_id=%s | actor=%s | fields=%s",
            incident_id, actor, sorted(applied),
        )
        return record

    @staticmethod
    def _require_curation_args(
        incident_id: str, reason: str, actor: str,
    ) -> tuple[str, str, str]:
        """Validate the three arguments MEM-4 makes mandatory for any curation."""
        incident_id = (incident_id or "").strip()
        reason = (reason or "").strip()
        actor = (actor or "").strip()
        if not incident_id:
            raise ValueError("incident_id must be a non-empty string.")
        if not reason:
            raise ValueError(
                "reason must be a non-empty string — MEM-4 requires every memory "
                "correction to record WHY it was made."
            )
        if not actor:
            raise ValueError(
                "actor must be a non-empty string — MEM-4 requires every memory "
                "correction to record WHO made it."
            )
        return incident_id, reason, actor

    def _find_memory_payload(self, incident_id: str) -> dict[str, Any] | None:
        """
        Return the stored payload of ``incident_id``'s memory, or ``None``.

        Uses Qdrant's scroll-by-filter (an exact payload lookup) rather than a
        similarity search, because "does a memory for this exact incident
        exist" is not a semantic question and a nearest-neighbour answer
        could return a DIFFERENT incident's memory.
        """
        try:
            from qdrant_client.http import models as qmodels

            points, _next = self._ingest._qdrant.scroll(
                collection_name=self._ingest.collection,
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="incident_id",
                            match=qmodels.MatchValue(value=incident_id),
                        )
                    ]
                ),
                limit=1,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "_find_memory_payload | lookup failed | incident_id=%s | error=%s",
                incident_id, exc,
            )
            return None

        if not points:
            return None
        return dict(getattr(points[0], "payload", None) or {})

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
