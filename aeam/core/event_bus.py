"""
aeam/core/event_bus.py

Internal synchronous event dispatcher for the AEAM modular monolith.

The EventBus decouples event producers (detectors, monitors) from event consumers
(investigators, notifiers, recorders) without introducing any external networking,
message broker dependency, or async complexity. Handlers are registered per
event_type and invoked synchronously in registration order when a matching event
is published.

Handler exceptions are isolated — a failing handler never prevents other handlers
for the same event from executing. Failed invocations are collected and surfaced
as a summary exception after all handlers have run.

This module contains no agent logic, no orchestrator references, and performs
no external I/O.
"""

import traceback
from collections import defaultdict
from typing import Callable

from aeam.core.event_models import Event


# Type alias for clarity throughout this module.
EventHandler = Callable[[Event], None]


class HandlerError(Exception):
    """
    Raised by :meth:`EventBus.publish` when one or more handlers fail.

    Wraps the individual exceptions so callers can inspect each failure
    independently while still receiving a single exception from ``publish``.

    Attributes:
        failures: List of ``(handler_name, exception)`` pairs, one per failed
                  handler, in the order they were invoked.
    """

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures: list[tuple[str, Exception]] = failures
        summary = "; ".join(
            f"{name!r} → {type(exc).__name__}: {exc}"
            for name, exc in failures
        )
        super().__init__(f"{len(failures)} handler(s) failed during publish: {summary}")


class EventBus:
    """
    Synchronous internal event dispatcher.

    Producers call :meth:`publish` with an :class:`~aeam.core.event_models.Event`;
    the bus fans the event out to every handler registered under that event's
    ``event_type``, plus any handlers registered under the wildcard ``"*"``.

    Handlers are plain callables with the signature ``(event: Event) -> None``.
    They are invoked in the order they were registered. A handler that raises
    an exception is caught and recorded; subsequent handlers are still called.
    After all handlers have run, :meth:`publish` raises :class:`HandlerError`
    if any failures occurred.

    No external networking, async, or agent logic is used.

    Example::

        bus = EventBus()

        def alert_on_critical(event: Event) -> None:
            if event.severity == "CRITICAL":
                send_alert(event)

        bus.register_handler("THRESHOLD_BREACH", alert_on_critical)
        bus.publish(event)
    """

    def __init__(self) -> None:
        """Initialise an EventBus with no registered handlers."""
        # defaultdict so we never need to guard for missing keys.
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_handler(self, event_type: str, handler: EventHandler) -> None:
        """
        Register a callable to be invoked when an event of ``event_type`` is published.

        Use the special event_type ``"*"`` to receive every event regardless of type.

        Args:
            event_type: The ``Event.event_type`` string this handler should
                        respond to, or ``"*"`` for a catch-all handler.
            handler:    Any callable accepting a single
                        :class:`~aeam.core.event_models.Event` argument and
                        returning ``None``. The same callable may be registered
                        multiple times and will be invoked once per registration.

        Raises:
            ValueError: If ``event_type`` is an empty or whitespace-only string.
            TypeError:  If ``handler`` is not callable.

        Example::

            bus.register_handler("ANOMALY", my_handler)
            bus.register_handler("*", audit_logger)
        """
        if not event_type or not event_type.strip():
            raise ValueError("event_type must be a non-empty string.")
        if not callable(handler):
            raise TypeError(
                f"handler must be callable, got {type(handler).__name__!r}."
            )

        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        """
        Dispatch ``event`` to all registered handlers for its type.

        Handlers registered under ``event.event_type`` are called first (in
        registration order), followed by wildcard (``"*"``) handlers. Each
        handler receives the original, unmodified ``Event`` object.

        Exception isolation:
            If a handler raises any exception it is caught, its traceback is
            captured, and execution continues with the next handler. Once all
            handlers have been called, a :class:`HandlerError` is raised
            summarising every failure. If no handler fails, ``publish`` returns
            ``None`` normally.

        Args:
            event: The :class:`~aeam.core.event_models.Event` to dispatch.
                   The event is never mutated.

        Raises:
            HandlerError: If one or more handlers raised an exception.

        Example::

            try:
                bus.publish(event)
            except HandlerError as exc:
                for handler_name, error in exc.failures:
                    record_failure(handler_name, error)
        """
        failures: list[tuple[str, Exception]] = []

        # Collect the specific handlers for this event_type, plus catch-all handlers.
        # Use a list to avoid mutating during iteration if a handler registers more.
        specific: list[EventHandler] = list(self._handlers.get(event.event_type, []))
        wildcard_star: list[EventHandler] = list(self._handlers.get("*", []))
        wildcard_all: list[EventHandler] = list(self._handlers.get("ALL", []))

        for handler in specific + wildcard_star + wildcard_all:
            self._invoke(handler, event, failures)

        if failures:
            raise HandlerError(failures)

    def handler_count(self, event_type: str | None = None) -> int:
        """
        Return the number of registered handlers.

        Args:
            event_type: If provided, count only handlers registered under this
                        specific event type. If ``None``, return the total count
                        across all event types.

        Returns:
            Integer count of registered handlers.
        """
        if event_type is not None:
            return len(self._handlers.get(event_type, []))
        return sum(len(handlers) for handlers in self._handlers.values())

    def registered_event_types(self) -> list[str]:
        """
        Return a sorted list of all event types that have at least one handler.

        Returns:
            Sorted list of event_type strings (may include ``"*"``).
        """
        return sorted(self._handlers.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _invoke(
        handler: EventHandler,
        event: Event,
        failures: list[tuple[str, Exception]],
    ) -> None:
        """
        Call ``handler(event)``, catching and recording any exception.

        Args:
            handler:  The handler callable to invoke.
            event:    The event to pass to the handler.
            failures: Mutable list to append ``(handler_name, exc)`` on failure.
        """
        handler_name = getattr(handler, "__qualname__", repr(handler))
        try:
            handler(event)
        except Exception as exc:  # noqa: BLE001
            # Attach the formatted traceback to the exception for later inspection.
            exc.__context_traceback__ = traceback.format_exc()  # type: ignore[attr-defined]
            failures.append((handler_name, exc))

    def __repr__(self) -> str:
        total = self.handler_count()
        types = self.registered_event_types()
        return f"EventBus(handlers={total}, event_types={types})"