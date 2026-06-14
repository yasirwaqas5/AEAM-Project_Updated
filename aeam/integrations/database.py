"""
aeam/integrations/database.py

SQLAlchemy-based database access layer for the AEAM modular monolith.

Provides a thin, synchronous client over a connection pool.
All queries must be parameterised — raw string interpolation is never used.
Exceptions are caught at each public method boundary, logged minimally, and
re-raised so that callers retain full control over error handling policy.

This module contains no agent logic, no business rules, and no ORM models.
It satisfies the ``DatabaseClient`` protocol defined in
``aeam/memory/long_term.py``.
"""

import json
import logging
import uuid
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)


class DatabaseClient:
    """
    Synchronous PostgreSQL access client backed by a SQLAlchemy ``QueuePool``.

    All public methods use parameterised queries via SQLAlchemy's
    :func:`sqlalchemy.text` construct, preventing SQL injection. Connections
    are drawn from a shared pool and returned automatically after each
    operation.

    This client satisfies the ``DatabaseClient`` protocol expected by
    :class:`~aeam.memory.long_term.LongTermMemory`.

    Args:
        database_url:    SQLAlchemy-compatible connection string
                         (e.g. ``"postgresql+psycopg2://user:pass@host/db"``).
        pool_size:       Number of persistent connections to maintain.
                         Defaults to ``5``.
        max_overflow:    Extra connections allowed beyond ``pool_size`` during
                         peak load. Defaults to ``10``.
        pool_timeout:    Seconds to wait for a connection before raising.
                         Defaults to ``30``.
        pool_recycle:    Seconds after which idle connections are recycled to
                         avoid server-side timeouts. Defaults to ``1800``
                         (30 minutes).

    Raises:
        SQLAlchemyError: If the engine cannot be created with the supplied URL.

    Example::

        client = DatabaseClient(
            database_url="postgresql+psycopg2://user:secret@localhost/aeam"
        )
        client.execute(
            "UPDATE incidents SET status = :status WHERE incident_id = :id",
            params={"status": "resolved", "id": "INC-42"},
        )
    """

    def __init__(
        self,
        database_url: str,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: int = 30,
        pool_recycle: int = 1800,
    ) -> None:
        """
        Initialise the client and create the connection pool.

        Args:
            database_url:  SQLAlchemy connection URL. Must not be empty.
            pool_size:     Persistent pool size. Must be >= 1.
            max_overflow:  Additional transient connections allowed. Must be >= 0.
            pool_timeout:  Seconds to wait for a free connection. Must be >= 1.
            pool_recycle:  Seconds before idle connections are recycled.

        Raises:
            ValueError:      If ``database_url`` is empty or whitespace-only.
            SQLAlchemyError: If the engine cannot be created.
        """
        if not database_url or not database_url.strip():
            raise ValueError("database_url must be a non-empty string.")

        self._engine: Engine = create_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            # Echo is intentionally disabled; debugging should use DB-level logs.
            echo=False,
        )

        # Ensure required tables exist (development convenience)
        self._create_tables_if_not_exist()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """
        Execute a write statement (INSERT, UPDATE, DELETE, DDL) with no return value.

        The statement is executed inside an auto-committed transaction. If the
        statement fails, the transaction is rolled back, the error is logged at
        ERROR level, and the exception is re-raised.

        Args:
            query:  A parameterised SQL string using SQLAlchemy's named-parameter
                    syntax (e.g. ``"UPDATE t SET col = :val WHERE id = :id"``).
                    Positional ``?`` or ``%s`` placeholders are not accepted.
            params: Mapping of parameter names to values. Pass ``None`` for
                    queries with no parameters.

        Raises:
            ValueError:      If ``query`` is empty or whitespace-only.
            SQLAlchemyError: On any database-level failure.

        Example::

            client.execute(
                "DELETE FROM events WHERE event_id = :id",
                params={"id": "abc-123"},
            )
        """
        self._validate_query(query)

        try:
            with self._engine.begin() as conn:
                conn.execute(text(query), params or {})
        except SQLAlchemyError as exc:
            logger.error("execute() failed | query=%r | error=%s", query, exc)
            raise

    def fetch_one(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Execute a SELECT query and return at most one row as a dict.

        Args:
            query:  A parameterised SQL SELECT string using named-parameter
                    syntax (e.g. ``"SELECT * FROM incidents WHERE id = :id"``).
            params: Mapping of parameter names to values. Pass ``None`` if the
                    query takes no parameters.

        Returns:
            A :class:`dict` mapping column names to values for the first result
            row, or ``None`` if the query produced no rows.

        Raises:
            ValueError:      If ``query`` is empty or whitespace-only.
            SQLAlchemyError: On any database-level failure.

        Example::

            row = client.fetch_one(
                "SELECT * FROM incidents WHERE incident_id = :id",
                params={"id": "INC-42"},
            )
            if row:
                print(row["severity"])
        """
        self._validate_query(query)

        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(query), params or {})
                row = result.mappings().first()
                return dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            logger.error("fetch_one() failed | query=%r | error=%s", query, exc)
            raise

    def insert(
        self,
        table: str,
        data: dict[str, Any],
        returning_column: str = "incident_id",
    ) -> str:
        """
        Insert a single row into ``table`` and return the value of ``returning_column``.

        Constructs a parameterised INSERT statement dynamically from ``data``'s
        keys. If the table expects a primary key named ``returning_column`` and it
        is not present in ``data``, a UUID is generated and injected automatically.

        The INSERT uses a ``RETURNING {returning_column}`` clause so the persisted
        ID is authoritative — if the DB overrides the value (e.g. via a trigger),
        the returned ID reflects that.

        **Important:** If the target database is SQLite, the ``RETURNING`` clause
        is supported only in SQLite 3.35+. For earlier versions, this method
        will fail. Adjust accordingly for your environment.

        Args:
            table:             Target table name. Must consist only of alphanumeric
                               characters and underscores to prevent SQL injection via
                               the table name (which cannot be parameterised).
            data:              Column → value mapping for the new row. Must not be empty.
                               Values must be Python primitives compatible with the column
                               types. If the primary key column (``returning_column``) is
                               absent, a UUID is generated and added.
            returning_column:  Name of the column to return (usually the primary key).
                               Defaults to ``"incident_id"`` for backward compatibility.

        Returns:
            The value of ``returning_column`` from the inserted row as a string.

        Raises:
            ValueError:      If ``table`` is invalid, ``data`` is empty, or
                             the RETURNING clause yields no result.
            SQLAlchemyError: On any database-level failure.

        Example::

            incident_id = client.insert(
                table="incidents",
                data={
                    "event_id": "abc-123",
                    "metric": "cpu_utilization",
                    "severity": "HIGH",
                    "timestamp": "2024-01-15T10:30:00Z",
                },
            )
            # returns the generated incident_id

            action_id = client.insert(
                table="action_logs",
                data={
                    "action_id": "123e4567-e89b-12d3-a456-426614174000",
                    "incident_id": "abc-123",
                    "action_type": "jira",
                    "parameters": "{\"summary\": \"...\"}",
                    "status": "SUCCESS",
                    "result": "{\"ticket_id\": \"PROJ-123\"}",
                    "executed_at": "2025-01-01T12:00:00Z",
                },
                returning_column="action_id",
            )
            # returns the action_id
        """
        self._validate_table_name(table)
        if not data:
            raise ValueError("data must be a non-empty dict to insert a row.")

        # Work on a copy to avoid mutating the caller's dict
        row = dict(data)

        # Ensure a stable primary key exists in the payload if it's absent.
        if returning_column not in row:
            row[returning_column] = str(uuid.uuid4())

        # Serialise any dict/list values to JSON strings for SQLite compatibility.
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                row[key] = json.dumps(value)

        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{col}" for col in row.keys())
        query = (
            f"INSERT INTO {table} ({columns}) "  # noqa: S608 — table name validated above
            f"VALUES ({placeholders}) "
            f"RETURNING {returning_column}"
        )

        try:
            with self._engine.begin() as conn:
                result = conn.execute(text(query), row)
                returned = result.mappings().first()

            if returned is None:
                raise ValueError(
                    f"INSERT into '{table}' succeeded but RETURNING clause "
                    f"returned no rows. Check table constraints."
                )

            return str(returned[returning_column])

        except SQLAlchemyError as exc:
            logger.error(
                "insert() failed | table=%r | data_keys=%s | error=%s",
                table,
                list(row.keys()),
                exc,
            )
            raise

    # ------------------------------------------------------------------
    # Convenience aliases to satisfy DatabaseClient protocol
    # ------------------------------------------------------------------

    def insert_incident(self, data: dict[str, Any]) -> str:
        """
        Insert a row into the ``incidents`` table and return the incident_id.

        Thin wrapper around :meth:`insert` that fixes the target table to
        ``"incidents"`` and the returning column to ``"incident_id"``.
        Satisfies the ``DatabaseClient`` protocol expected by
        :class:`~aeam.memory.long_term.LongTermMemory`.

        Args:
            data: Column → value mapping for the new incident row.

        Returns:
            The ``incident_id`` of the newly inserted row.
        """
        return self.insert(table="incidents", data=data, returning_column="incident_id")

    def insert_decision(
        self,
        incident_id: str,
        decision: dict[str, Any],
    ) -> None:
        """
        Insert a decision record into the ``decisions`` table.

        Merges ``incident_id`` into the payload and delegates to
        :meth:`execute`. Satisfies the ``DatabaseClient`` protocol.

        Args:
            incident_id: Parent incident identifier.
            decision:    Decision fields to persist.
        """
        payload = {"incident_id": incident_id, **decision}
        # Serialise any complex values (though decisions likely have simple types)
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                payload[key] = json.dumps(value)

        columns = ", ".join(payload.keys())
        placeholders = ", ".join(f":{col}" for col in payload.keys())
        query = f"INSERT INTO decisions ({columns}) VALUES ({placeholders})"
        self.execute(query, params=payload)

    def insert_metrics(self, metrics: list[dict[str, Any]]) -> None:
        """
        Bulk-insert metric snapshot rows into the ``metrics`` table.

        Each metric dict is inserted as a separate parameterised statement
        within a single transaction. Satisfies the ``DatabaseClient`` protocol.

        Args:
            metrics: List of metric record dicts. Each must share the same
                     set of keys (consistent column schema).
        """
        if not metrics:
            return

        for metric in metrics:
            # Serialise any complex values (though metrics likely only have scalars)
            row = dict(metric)
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    row[key] = json.dumps(value)

            columns = ", ".join(row.keys())
            placeholders = ", ".join(f":{col}" for col in row.keys())
            query = f"INSERT INTO metrics ({columns}) VALUES ({placeholders})"
            self.execute(query, params=row)

    def fetch_metric_history(
        self,
        metric_name: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch historical metric rows for a given metric.

        Returns a list of dicts ordered ascending by timestamp, each containing
        ``timestamp`` (datetime or ISO‑8601 string) and ``value`` (float).

        Args:
            metric_name: Name of the metric to retrieve history for.
            limit:       Maximum number of rows to return (most recent).
                         If ``None``, return all available history.

        Returns:
            List of metric records, each as a dict with keys ``timestamp``
            and ``value``.

        Raises:
            ValueError: If ``metric_name`` is empty or whitespace-only.
            SQLAlchemyError: On database failure.
        """
        if not metric_name or not metric_name.strip():
            raise ValueError("metric_name must be a non-empty string.")

        query = """
            SELECT timestamp, value
            FROM metrics
            WHERE metric = :metric_name
            ORDER BY timestamp ASC
        """
        params = {"metric_name": metric_name}

        if limit is not None:
            query += " LIMIT :limit"
            params["limit"] = limit

        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(query), params)
                rows = result.fetchall()

            return [
                {"timestamp": row[0], "value": float(row[1])}
                for row in rows
            ]
        except SQLAlchemyError as exc:
            logger.error(
                "fetch_metric_history failed | metric=%s | error=%s",
                metric_name, exc,
            )
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_query(query: str) -> None:
        """
        Raise ``ValueError`` if ``query`` is empty or whitespace-only.

        Args:
            query: The SQL query string to validate.

        Raises:
            ValueError: If the query is empty or blank.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty SQL string.")

    @staticmethod
    def _validate_table_name(table: str) -> None:
        """
        Raise ``ValueError`` if ``table`` contains characters outside
        ``[a-zA-Z0-9_]``, guarding against SQL injection via the table name
        (table names cannot be parameterised in SQLAlchemy's ``text()``).

        Args:
            table: The table name string to validate.

        Raises:
            ValueError: If the table name is empty or contains invalid characters.
        """
        if not table or not table.strip():
            raise ValueError("table must be a non-empty string.")
        if not all(c.isalnum() or c == "_" for c in table):
            raise ValueError(
                f"table name must contain only alphanumeric characters and "
                f"underscores. Got: '{table}'."
            )

    def _create_tables_if_not_exist(self) -> None:
        """
        Create required AEAM tables if they do not exist.

        This is a development convenience, not a production migration tool.
        Tables are created with a schema compatible with SQLite and PostgreSQL.
        For PostgreSQL, the column types are valid; for SQLite, the type
        names are ignored (SQLite uses type affinity) but the structure works.

        Important:
            - ``findings`` and ``detection_methods`` are stored as TEXT and must
              be JSON-encoded before insertion (handled automatically in insert()).
            - If using PostgreSQL, you may later alter these to JSONB for native
              JSON support.
        """
        create_incidents = """
        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            event_id TEXT,
            event_type TEXT,
            metric TEXT,
            severity TEXT,
            current_value REAL,
            expected_value REAL,
            detection_methods TEXT,
            timestamp TEXT,
            investigation_depth INTEGER,
            root_cause TEXT,
            confidence REAL,
            action_taken BOOLEAN,
            requires_human BOOLEAN,
            findings TEXT
        );
        """

        create_decisions = """
        CREATE TABLE IF NOT EXISTS decisions (
            incident_id TEXT,
            decision TEXT,
            confidence REAL
        );
        """

        create_metrics = """
        CREATE TABLE IF NOT EXISTS metrics (
            metric TEXT,
            value REAL,
            timestamp TEXT
        );
        """

        # Action logs table for Phase 6 – matches ActionAgent._log_to_database()
        create_action_logs = """
        CREATE TABLE IF NOT EXISTS action_logs (
            action_id TEXT PRIMARY KEY,
            incident_id TEXT,
            action_type TEXT,
            parameters JSONB,
            status TEXT,
            result JSONB,
            executed_at TIMESTAMP
        );
        """

        try:
            with self._engine.begin() as conn:
                conn.execute(text(create_incidents))
                conn.execute(text(create_decisions))
                conn.execute(text(create_metrics))
                conn.execute(text(create_action_logs))
            logger.info("Database tables verified/created successfully.")
        except SQLAlchemyError as exc:
            logger.error("Table creation failed: %s", exc)
            # Re-raise because without tables the client is unusable.
            raise

    def dispose(self) -> None:
        """
        Dispose of the connection pool, closing all active and idle connections.

        Should be called during application shutdown to release database
        resources cleanly.
        """
        self._engine.dispose()
        logger.info("DatabaseClient pool disposed.")

    def __repr__(self) -> str:
        pool = self._engine.pool
        return (
            f"DatabaseClient("
            f"pool_size={pool.size()}, "
            f"checked_out={pool.checkedout()}, "
            f"overflow={pool.overflow()})"
        )