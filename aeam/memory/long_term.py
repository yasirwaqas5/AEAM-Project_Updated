"""
aeam/memory/long_term.py

Long-Term Memory (LTM) interface for the AEAM modular monolith.

LTM provides a stable, append-oriented API for persisting investigation
artefacts — incidents, decisions, and metrics — across task boundaries.
All persistence is delegated entirely to injected client objects; this module
contains no SQL, no query construction, no LLM calls, and no business logic.

Two storage backends are supported:
- ``database_client`` — a relational (PostgreSQL) client for structured records.
- ``vector_client``   — a vector database client for embedding-based retrieval.

Both clients are dependency-injected, keeping this class decoupled from any
specific driver, ORM, or connection pool implementation.
"""

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Client protocols (optional, kept for type hints but no runtime isinstance)
# ---------------------------------------------------------------------------


@runtime_checkable
class DatabaseClient(Protocol):
    """
    Structural protocol for a relational database client.

    Any object that implements these methods is a valid ``database_client``
    for :class:`LongTermMemory`. No specific driver (psycopg2, asyncpg,
    SQLAlchemy, etc.) is assumed.
    """

    def insert_incident(self, data: dict[str, Any]) -> str:
        """
        Insert a new row into the incidents table.

        Args:
            data: Mapping of column names to values for the new incident row.

        Returns:
            The ``incident_id`` of the newly created record, as a string.
        """
        ...

    def insert_decision(self, incident_id: str, decision: dict[str, Any]) -> None:
        """
        Append a decision record associated with ``incident_id``.

        Args:
            incident_id: Identifier of the parent incident.
            decision:    Mapping of decision fields to store.
        """
        ...

    def insert_metrics(self, metrics: list[dict[str, Any]]) -> None:
        """
        Bulk-insert a list of metric snapshots.

        Args:
            metrics: List of metric record dicts to insert.
        """
        ...

    def fetch_metric_history(
        self,
        metric_name: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch historical metric rows for a given metric.

        The returned list must be ordered ascending by timestamp.
        Each dict must contain at least:
            - timestamp (datetime or ISO‑8601 string)
            - value     (float)

        Args:
            metric_name: Name of the metric to retrieve history for.
            limit:       Maximum number of rows to return (most recent).
                         If ``None``, return all available history.

        Returns:
            List of metric records, each as a dict with at least
            ``timestamp`` and ``value`` keys.
        """
        ...


@runtime_checkable
class VectorClient(Protocol):
    """
    Structural protocol for a vector database client.

    Any object that implements these methods is a valid ``vector_client``
    for :class:`LongTermMemory`. No specific vector DB (Qdrant, Weaviate,
    Pinecone, etc.) is assumed.
    """

    def upsert(self, collection: str, payload: dict[str, Any]) -> None:
        """
        Upsert a document payload into the named collection.

        Args:
            collection: Name of the target collection / index.
            payload:    Document data to upsert, including any pre-computed
                        embedding vectors expected by the backend.
        """
        ...


# ---------------------------------------------------------------------------
# Long-Term Memory
# ---------------------------------------------------------------------------


class LongTermMemory:
    """
    Persistence interface for AEAM investigation artefacts.

    Provides three append-oriented methods that delegate all I/O to injected
    client objects. This class contains no SQL, no query construction, no
    embedding logic, no LLM calls, and no business rules.

    Args:
        database_client: An object conforming to :class:`DatabaseClient` — used
                         for structured relational persistence (incidents,
                         decisions, metrics).
        vector_client:   An object conforming to :class:`VectorClient` — used
                         for vector storage to support embedding-based retrieval
                         of historical incidents and decisions.

    Raises:
        TypeError: At construction time if either client does not satisfy its
                   expected protocol.

    Example::

        ltm = LongTermMemory(
            database_client=pg_client,
            vector_client=qdrant_client,
        )

        incident_id = ltm.record_incident({"metric": "cpu", "severity": "HIGH"})
        ltm.log_decision(incident_id, {"action": "scale_up", "confidence": 0.9})
        ltm.store_metrics([{"metric": "cpu", "value": 97.4, "ts": "..."}])

        # New in Phase 5:
        history = ltm.get_metric_history("cpu", limit=100)
    """

    def __init__(
        self,
        database_client: DatabaseClient,
        vector_client: VectorClient,
    ) -> None:
        """
        Initialise LongTermMemory with injected storage clients.

        Args:
            database_client: Relational DB client conforming to
                             :class:`DatabaseClient`.
            vector_client:   Vector DB client conforming to
                             :class:`VectorClient`.

        Raises:
            TypeError: If ``database_client`` does not satisfy
                       :class:`DatabaseClient`, or ``vector_client`` does not
                       satisfy :class:`VectorClient`.
        """
        # Duck‑typing: verify required methods exist (no isinstance on protocols)
        required_db_methods = ["insert_incident", "insert_decision", "insert_metrics", "fetch_metric_history"]
        for method in required_db_methods:
            if not hasattr(database_client, method):
                raise TypeError(
                    f"database_client missing required method '{method}'. "
                    f"Got: {type(database_client).__name__!r}."
                )

        required_vector_methods = ["upsert"]
        for method in required_vector_methods:
            if not hasattr(vector_client, method):
                raise TypeError(
                    f"vector_client missing required method '{method}'. "
                    f"Got: {type(vector_client).__name__!r}."
                )

        self._db: DatabaseClient = database_client
        self._vector: VectorClient = vector_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_incident(self, data: dict[str, Any]) -> str:
        """
        Persist a new incident to the relational store and the vector store.

        Delegates the relational insert to ``database_client.insert_incident``,
        which is responsible for writing to the ``incidents`` table and returning
        the generated ``incident_id``. The same payload is then upserted to the
        ``"incidents"`` vector collection to support future embedding-based
        retrieval.

        This method performs no data transformation, validation, or enrichment —
        ``data`` is forwarded to the clients as-is.

        Args:
            data: Mapping of incident fields to persist. The expected schema is
                  defined by the database client's underlying table / collection.
                  Typical keys include ``event_id``, ``event_type``, ``metric``,
                  ``severity``, and ``timestamp``.

        Returns:
            The ``incident_id`` string returned by the database client.

        Raises:
            ValueError: If ``data`` is empty.
            Exception:  Any exception raised by ``database_client.insert_incident``
                        or ``vector_client.upsert`` is propagated to the caller
                        without wrapping, preserving the original traceback.

        Example::

            incident_id = ltm.record_incident({
                "event_id": "abc-123",
                "event_type": "THRESHOLD_BREACH",
                "metric": "cpu_utilization",
                "severity": "HIGH",
                "timestamp": "2024-01-15T10:30:00Z",
            })
        """
        if not data:
            raise ValueError("data must be a non-empty dict to record an incident.")

        incident_id: str = self._db.insert_incident(data)

        # Mirror to vector store; callers expecting embeddings should enrich
        # `data` with a vector field before calling record_incident.
        self._vector.upsert(
            collection="incidents",
            payload={"incident_id": incident_id, **data},
        )

        return incident_id

    def log_decision(self, incident_id: str, decision: dict[str, Any]) -> None:
        """
        Append a decision record linked to an existing incident.

        Delegates the relational insert to ``database_client.insert_decision``
        and mirrors the record to the ``"decisions"`` vector collection.

        This method performs no validation of the decision content or the
        existence of the referenced incident — that is the responsibility of
        the database client and its underlying constraints.

        Args:
            incident_id: Identifier of the incident this decision relates to.
                         Must be a non-empty string.
            decision:    Mapping of decision fields. Typical keys include
                         ``action``, ``rationale``, ``confidence``, and
                         ``decided_at``.

        Raises:
            ValueError: If ``incident_id`` is empty or whitespace-only, or if
                        ``decision`` is empty.
            Exception:  Any exception raised by the underlying clients is
                        propagated without wrapping.

        Example::

            ltm.log_decision(
                incident_id="INC-42",
                decision={
                    "action": "scale_up",
                    "rationale": "CPU sustained above 90% for 10 minutes.",
                    "confidence": 0.91,
                    "decided_at": "2024-01-15T10:35:00Z",
                },
            )
        """
        if not incident_id or not incident_id.strip():
            raise ValueError("incident_id must be a non-empty string.")
        if not decision:
            raise ValueError("decision must be a non-empty dict.")

        self._db.insert_decision(incident_id, decision)

        self._vector.upsert(
            collection="decisions",
            payload={"incident_id": incident_id, **decision},
        )

    def store_metrics(self, metrics: list[dict[str, Any]]) -> None:
        """
        Bulk-persist a list of metric snapshots to the relational store.

        Delegates directly to ``database_client.insert_metrics``. No vector
        upsert is performed for raw metrics — they are relational-only.

        This method performs no transformation or aggregation of the metrics
        list. An empty list is a no-op.

        Args:
            metrics: List of metric snapshot dicts to persist. Each dict
                     typically contains keys such as ``metric``, ``value``,
                     and ``timestamp``. An empty list is accepted and results
                     in no database call.

        Raises:
            Exception: Any exception raised by ``database_client.insert_metrics``
                       is propagated without wrapping.

        Example::

            ltm.store_metrics([
                {"metric": "cpu_utilization", "value": 97.4, "timestamp": "..."},
                {"metric": "memory_usage",    "value": 84.1, "timestamp": "..."},
            ])
        """
        if not metrics:
            return

        self._db.insert_metrics(metrics)

    def get_metric_history(
        self,
        metric_name: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve historical values for a given metric.

        The data is fetched exclusively from the relational database via the
        injected ``database_client``. No vector store is involved.

        Args:
            metric_name: Name of the metric to retrieve history for.
            limit:       Maximum number of records to return (most recent).
                         If ``None``, all available history is returned.

        Returns:
            List of metric records, each containing at least ``timestamp``
            and ``value`` keys, ordered ascending by timestamp.

        Raises:
            ValueError: If ``metric_name`` is empty or whitespace-only.
            Exception:  Any exception raised by the underlying database client
                        is propagated without wrapping.

        Example::

            history = ltm.get_metric_history("cpu_utilization", limit=100)
            # [
            #   {"timestamp": "2025-01-01T00:00:00Z", "value": 42.0},
            #   {"timestamp": "2025-01-01T01:00:00Z", "value": 43.5},
            #   ...
            # ]
        """
        if not metric_name or not metric_name.strip():
            raise ValueError("metric_name must be a non-empty string.")

        return self._db.fetch_metric_history(metric_name, limit)

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"LongTermMemory("
            f"database_client={self._db!r}, "
            f"vector_client={self._vector!r})"
        )