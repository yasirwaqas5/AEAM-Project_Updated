"""
aeam/memory/short_term.py

Short-Term Memory (STM) implementation for the AEAM modular monolith.

STM holds transient, in-process state for a single investigation task. It is
initialised at the start of a task, mutated as findings accumulate, and
discarded when the task completes. Nothing is written to disk or any external
store — the backing dict lives entirely in process memory.

The :meth:`ShortTermMemory.serialize_for_llm` method produces a compact JSON
snapshot of only the fields relevant to LLM prompting (event, findings,
hypotheses, confidence), keeping token usage predictable and bounded.
"""

import json
from datetime import datetime, timezone
from typing import Any


# Keys extracted when serialising for LLM consumption.
_LLM_KEYS: tuple[str, ...] = ("event", "findings", "hypotheses", "confidence")


class ShortTermMemory:
    """
    Ephemeral, in-process memory for a single AEAM investigation task.

    STM is initialised once per task via :meth:`initialize`, then used as a
    simple key-value store throughout the task lifecycle. It is intentionally
    non-persistent: no database, no disk I/O, no external references.

    The internal store is a plain Python :class:`dict`. Callers may store any
    JSON-serialisable value. The :meth:`serialize_for_llm` method returns a
    compact JSON string containing only the four LLM-relevant keys:
    ``event``, ``findings``, ``hypotheses``, and ``confidence``.

    Example::

        stm = ShortTermMemory()
        stm.initialize(task_type="anomaly_investigation", incident_id="INC-42")
        stm.set("event", event.model_dump())
        stm.append("findings", {"check": "cpu_spike", "result": "confirmed"})
        stm.set("confidence", 0.87)

        prompt_context = stm.serialize_for_llm()
    """

    def __init__(self) -> None:
        """Initialise a blank ShortTermMemory instance with no active task."""
        self._store: dict[str, Any] = {}
        self._initialised: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(
        self,
        task_type: str,
        incident_id: str | None = None,
    ) -> None:
        """
        Prepare the STM for a new task, clearing any previous state.

        Seeds the store with task metadata and empty containers for the
        four LLM-relevant keys so callers can safely ``append`` or ``get``
        them without checking for prior existence.

        Args:
            task_type:   Short label describing the kind of task being run
                         (e.g. ``"anomaly_investigation"``, ``"threshold_check"``).
                         Must not be empty or whitespace-only.
            incident_id: Optional external incident identifier to correlate
                         this task with an incident management system.

        Raises:
            ValueError: If ``task_type`` is empty or whitespace-only.
        """
        if not task_type or not task_type.strip():
            raise ValueError("task_type must be a non-empty string.")

        self._store = {
            # Task metadata — not sent to LLM.
            "_task_type": task_type.strip(),
            "_incident_id": incident_id,
            "_initialized_at": datetime.now(tz=timezone.utc).isoformat(),
            # LLM-relevant keys seeded with neutral defaults.
            "event": None,
            "findings": [],
            "hypotheses": [],
            "confidence": None,
        }
        self._initialised = True

    def clear(self) -> None:
        """
        Erase all stored state and mark the STM as uninitialised.

        After calling ``clear``, :meth:`initialize` must be called before
        the STM can be used again. This method is idempotent — calling it
        on an already-cleared STM is safe.
        """
        self._store = {}
        self._initialised = False

    # ------------------------------------------------------------------
    # Core accessors
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve a value from the store by key.

        Args:
            key:     The key to look up.
            default: Value to return if ``key`` is absent. Defaults to ``None``.

        Returns:
            The stored value, or ``default`` if the key does not exist.

        Raises:
            RuntimeError: If called before :meth:`initialize`.
        """
        self._require_initialised("get")
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        Store a value under ``key``, overwriting any existing value.

        Args:
            key:   The key to write. May be any non-empty string. Keys
                   prefixed with ``_`` are reserved for internal metadata
                   and will raise a :class:`ValueError` if written directly.
            value: Any value. For :meth:`serialize_for_llm` to work correctly,
                   values stored under the LLM-relevant keys (``event``,
                   ``findings``, ``hypotheses``, ``confidence``) must be
                   JSON-serialisable.

        Raises:
            RuntimeError: If called before :meth:`initialize`.
            ValueError:   If ``key`` is empty, whitespace-only, or starts with ``_``.
        """
        self._require_initialised("set")
        self._validate_key(key)
        self._store[key] = value

    def append(self, key: str, value: Any) -> None:
        """
        Append ``value`` to the list stored under ``key``.

        If ``key`` does not exist, a new list is created containing ``value``.
        If ``key`` exists but its current value is not a :class:`list`, a
        :class:`TypeError` is raised to prevent silent data corruption.

        Designed for accumulating findings and hypotheses over multiple
        investigation steps.

        Args:
            key:   The key whose list should be extended.
            value: The item to append.

        Raises:
            RuntimeError: If called before :meth:`initialize`.
            ValueError:   If ``key`` is empty, whitespace-only, or starts with ``_``.
            TypeError:    If the existing value under ``key`` is not a list.
        """
        self._require_initialised("append")
        self._validate_key(key)

        existing = self._store.get(key)
        if existing is None:
            self._store[key] = [value]
        elif isinstance(existing, list):
            existing.append(value)
        else:
            raise TypeError(
                f"Cannot append to key '{key}': existing value is "
                f"{type(existing).__name__!r}, expected list."
            )

    # ------------------------------------------------------------------
    # LLM serialisation
    # ------------------------------------------------------------------

    def serialize_for_llm(self) -> str:
        """
        Produce a compact JSON snapshot of LLM-relevant memory contents.

        Only the four keys meaningful to an LLM prompt are included:
        ``event``, ``findings``, ``hypotheses``, and ``confidence``. Internal
        metadata keys (prefixed with ``_``) and any other application keys are
        excluded, keeping the serialised payload focused and token-efficient.

        Returns:
            A JSON string with ``ensure_ascii=False`` and no unnecessary
            whitespace, suitable for direct injection into an LLM prompt.

        Raises:
            RuntimeError:      If called before :meth:`initialize`.
            ValueError:        If any LLM-relevant value is not JSON-serialisable.

        Example output::

            {
              "event": {"event_id": "abc-123", "event_type": "THRESHOLD_BREACH", ...},
              "findings": [{"check": "cpu_spike", "result": "confirmed"}],
              "hypotheses": ["Memory leak in service A", "Noisy neighbour on host"],
              "confidence": 0.87
            }
        """
        self._require_initialised("serialize_for_llm")

        snapshot: dict[str, Any] = {
            key: self._store.get(key) for key in _LLM_KEYS
        }

        try:
            return json.dumps(snapshot, ensure_ascii=False, default=_json_default)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"serialize_for_llm failed: one or more LLM-relevant values "
                f"are not JSON-serialisable. Detail: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_initialised(self, method_name: str) -> None:
        """
        Guard that raises :class:`RuntimeError` if the STM has not been initialised.

        Args:
            method_name: Name of the calling method, used in the error message.

        Raises:
            RuntimeError: If :meth:`initialize` has not been called since the
                          last :meth:`clear` (or since construction).
        """
        if not self._initialised:
            raise RuntimeError(
                f"ShortTermMemory.{method_name}() called before initialize(). "
                "Call initialize(task_type=...) first."
            )

    @staticmethod
    def _validate_key(key: str) -> None:
        """
        Reject keys that are empty, whitespace-only, or use the reserved ``_`` prefix.

        Args:
            key: The key string to validate.

        Raises:
            ValueError: If the key is invalid.
        """
        if not key or not key.strip():
            raise ValueError("Key must be a non-empty, non-whitespace string.")
        if key.startswith("_"):
            raise ValueError(
                f"Keys prefixed with '_' are reserved for internal metadata. "
                f"Got: '{key}'."
            )

    def __repr__(self) -> str:
        if not self._initialised:
            return "ShortTermMemory(uninitialised)"
        task_type = self._store.get("_task_type", "unknown")
        incident_id = self._store.get("_incident_id")
        keys = [k for k in self._store if not k.startswith("_")]
        return (
            f"ShortTermMemory("
            f"task_type={task_type!r}, "
            f"incident_id={incident_id!r}, "
            f"keys={keys})"
        )


# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """
    Fallback serialiser for types not natively handled by :func:`json.dumps`.

    Currently handles:
    - :class:`datetime` → ISO-8601 string.
    - Objects with a ``model_dump`` method (Pydantic models) → dict.
    - Objects with a ``__dict__`` attribute → dict.

    Args:
        obj: The non-serialisable object.

    Returns:
        A JSON-serialisable representation.

    Raises:
        TypeError: If no conversion is possible.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj).__name__!r} is not JSON-serialisable.")