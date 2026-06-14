"""
aeam/core/priority_queue.py

Priority classification and in-memory priority queue for AEAM events.

Events are ranked by severity (CRITICAL → HIGH → MEDIUM → LOW). Within the same
priority tier, strict FIFO ordering is preserved via a monotonic insertion counter.
All public methods are thread-safe; a single threading.Lock guards the underlying
heap so the queue can be shared safely across producer and consumer threads.
"""

import heapq
import itertools
import threading
from enum import IntEnum
from typing import Iterator

from aeam.core.event_models import Event


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


class PriorityLevel(IntEnum):
    """
    Numeric priority levels for event processing.

    Lower integer values are dequeued first by heapq (min-heap semantics).

    Members:
        CRITICAL: Highest urgency — processed first.
        HIGH:     Elevated urgency.
        MEDIUM:   Normal urgency.
        LOW:      Background / informational — processed last.
    """

    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


# Mapping from Event.severity strings to PriorityLevel.
# Defined at module level so it can be imported and reused by other modules.
SEVERITY_TO_PRIORITY: dict[str, PriorityLevel] = {
    "CRITICAL": PriorityLevel.CRITICAL,
    "HIGH": PriorityLevel.HIGH,
    "MEDIUM": PriorityLevel.MEDIUM,
    "LOW": PriorityLevel.LOW,
}


# ---------------------------------------------------------------------------
# Internal heap entry
# ---------------------------------------------------------------------------


class _HeapEntry:
    """
    Internal wrapper stored inside the heap.

    Heap comparison uses (priority, sequence) as a composite key:
    - ``priority``  — lower value = higher urgency (matches PriorityLevel ints).
    - ``sequence``  — monotonically increasing counter; breaks ties so that
                      events at the same priority level are dequeued in FIFO order.

    The wrapped ``Event`` is intentionally excluded from comparison to avoid
    errors when two events happen to have equal numeric fields.

    Attributes:
        priority:  PriorityLevel int value for this entry.
        sequence:  Insertion counter assigned at push time.
        event:     The underlying immutable Event object.
    """

    __slots__ = ("priority", "sequence", "event")

    def __init__(self, priority: PriorityLevel, sequence: int, event: Event) -> None:
        self.priority = priority
        self.sequence = sequence
        self.event = event

    def __lt__(self, other: "_HeapEntry") -> bool:
        return (self.priority, self.sequence) < (other.priority, other.sequence)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _HeapEntry):
            return NotImplemented
        return (self.priority, self.sequence) == (other.priority, other.sequence)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class EventPriorityQueue:
    """
    Thread-safe, in-memory priority queue for :class:`~aeam.core.event_models.Event` objects.

    Events are dequeued in severity order (CRITICAL first, LOW last). Events sharing
    the same severity level are dequeued in FIFO order relative to when they were
    pushed.

    This class contains no agent or orchestrator logic — it is a pure data structure.

    Example::

        queue = EventPriorityQueue()
        queue.push(low_event)
        queue.push(critical_event)

        next_event = queue.pop()  # returns critical_event
    """

    def __init__(self) -> None:
        """Initialise an empty priority queue."""
        self._heap: list[_HeapEntry] = []
        self._lock: threading.Lock = threading.Lock()
        # itertools.count() is not itself thread-safe for concurrent reads,
        # but we always call next() under self._lock, so this is safe.
        self._counter: Iterator[int] = itertools.count()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, event: Event) -> None:
        """
        Add an event to the queue.

        The event's ``severity`` field is mapped to a :class:`PriorityLevel`.
        Events with higher urgency (lower PriorityLevel value) will be returned
        before events with lower urgency, regardless of insertion order.

        Args:
            event: The :class:`~aeam.core.event_models.Event` to enqueue.

        Raises:
            KeyError: If ``event.severity`` is not a recognised severity string.
                      (Should never happen if the Event was properly validated,
                      but guarded here for defence-in-depth.)

        Thread safety:
            Safe to call concurrently from multiple threads.
        """
        priority = self._resolve_priority(event)
        with self._lock:
            sequence = next(self._counter)
            entry = _HeapEntry(priority=priority, sequence=sequence, event=event)
            heapq.heappush(self._heap, entry)

    def pop(self) -> Event:
        """
        Remove and return the highest-priority event.

        When multiple events share the same priority level, the one that was
        pushed earliest (FIFO) is returned first.

        Returns:
            The :class:`~aeam.core.event_models.Event` with the highest urgency.

        Raises:
            IndexError: If the queue is empty.

        Thread safety:
            Safe to call concurrently from multiple threads.
        """
        with self._lock:
            if not self._heap:
                raise IndexError("pop from an empty EventPriorityQueue")
            entry = heapq.heappop(self._heap)
        return entry.event

    def is_empty(self) -> bool:
        """
        Return ``True`` if the queue contains no events.

        Thread safety:
            Safe to call concurrently from multiple threads. Note that the result
            may be stale by the time the caller acts on it — check the return value
            of :meth:`pop` rather than relying on ``is_empty`` as a guard in
            concurrent contexts.
        """
        with self._lock:
            return len(self._heap) == 0

    def size(self) -> int:
        """
        Return the current number of events in the queue.

        Thread safety:
            Safe to call concurrently from multiple threads. Subject to the same
            TOCTOU caveat as :meth:`is_empty`.
        """
        with self._lock:
            return len(self._heap)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_priority(event: Event) -> PriorityLevel:
        """
        Map an event's severity string to a :class:`PriorityLevel`.

        Args:
            event: The event whose severity should be resolved.

        Returns:
            The corresponding :class:`PriorityLevel`.

        Raises:
            KeyError: If the severity value is not present in
                      :data:`SEVERITY_TO_PRIORITY`.
        """
        try:
            return SEVERITY_TO_PRIORITY[event.severity]
        except KeyError:
            valid = sorted(SEVERITY_TO_PRIORITY.keys())
            raise KeyError(
                f"Unknown severity '{event.severity}'. "
                f"Expected one of: {valid}"
            ) from None

    def __repr__(self) -> str:
        return f"EventPriorityQueue(size={self.size()})"