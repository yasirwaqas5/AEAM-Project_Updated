"""
aeam/monitoring/tracing.py

Distributed tracing for the investigation path (Phase E11, OBS-1/OBS-6).

**This is not a second metrics pipeline.** ``aeam/monitoring/metrics.py``
remains the single metrics pipeline (OBS-1); OpenTelemetry spans are a
*distinct, complementary* signal — correlation and latency structure across
the stages of one investigation, which a Prometheus histogram fundamentally
cannot express. TECH-1 is satisfied because it retires the audit's
"no request/investigation tracing" gap and nothing already in the tree can
produce a span tree.

Design constraints this module honours:

- **Optional at runtime.** OpenTelemetry is an optional dependency. When the
  SDK is not installed, or tracing is not enabled by configuration, every
  function here is a cheap no-op and AEAM behaves exactly as it did before
  this phase (COMPAT-1). Nothing in the investigation path may fail because
  a tracing backend is absent, misconfigured, or unreachable.
- **OTLP standard export only.** No vendor SDK, no proprietary backend — the
  exporter speaks OTLP so any enterprise tracing backend (Tempo, Jaeger,
  Honeycomb, Datadog, Cloud Trace…) can consume it.
- **Incident id is the correlation key.** Every span carries
  ``aeam.incident_id``, the same value already present on the investigation
  path's log lines, so a trace and its logs join without a second
  correlation scheme.
- **Never raises.** A tracing failure is logged at DEBUG and swallowed. The
  investigation is the product; the trace is telemetry about it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: Span attribute carrying the incident id — the correlation key shared with
#: the investigation path's structured log lines.
INCIDENT_ID_ATTRIBUTE: str = "aeam.incident_id"

#: Instrumentation scope name for every span AEAM emits.
TRACER_NAME: str = "aeam.investigation"

_tracer: Any | None = None
_configured: bool = False
_status_ok: Any = None
_status_error: Any = None


def configure_tracing(settings: Any) -> bool:
    """
    Configure the OTLP tracer provider from ``settings``, once per process.

    Called from the application lifespan. Idempotent: repeated calls after a
    successful configuration are no-ops, so a test that builds several apps
    never stacks exporters.

    Args:
        settings: A :class:`~aeam.config.settings.Settings` (or any object
                  exposing ``OTEL_TRACING_ENABLED``,
                  ``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_SERVICE_NAME``,
                  and ``ENVIRONMENT``).

    Returns:
        ``True`` when a real tracer is now active; ``False`` when tracing
        stays disabled (not enabled by configuration, or the OpenTelemetry
        SDK is not installed). ``False`` is a completely normal outcome —
        it is the default posture.
    """
    global _tracer, _configured, _status_ok, _status_error

    if _configured:
        return _tracer is not None

    try:
        enabled = bool(getattr(settings, "OTEL_TRACING_ENABLED", False))
    except Exception as exc:  # noqa: BLE001
        # Reading configuration must not be able to abort startup either —
        # a settings object that raises is still a telemetry problem, and
        # telemetry never takes the platform down.
        _configured = True
        logger.error("Tracing configuration unreadable (%s). Tracing stays OFF.", exc)
        return False

    if not enabled:
        _configured = True
        logger.info(
            "Tracing disabled (OTEL_TRACING_ENABLED=false). Investigation spans "
            "are not emitted; Prometheus metrics are unaffected."
        )
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace import Status, StatusCode
    except ImportError as exc:
        _configured = True
        logger.warning(
            "OTEL_TRACING_ENABLED=true but the OpenTelemetry SDK is not "
            "installed (%s). Tracing stays OFF -- install the optional "
            "'opentelemetry-sdk' and 'opentelemetry-exporter-otlp-proto-http' "
            "packages to enable it. No other behaviour changes.",
            exc,
        )
        return False

    try:
        endpoint = str(getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        _configured = True
        logger.error(
            "OTEL_EXPORTER_OTLP_ENDPOINT unreadable (%s). Tracing stays OFF.", exc
        )
        return False

    if not endpoint:
        _configured = True
        logger.warning(
            "OTEL_TRACING_ENABLED=true but OTEL_EXPORTER_OTLP_ENDPOINT is "
            "empty. Tracing stays OFF rather than silently dropping spans."
        )
        return False

    try:
        resource = Resource.create({
            "service.name": str(getattr(settings, "OTEL_SERVICE_NAME", "aeam") or "aeam"),
            "deployment.environment": str(getattr(settings, "ENVIRONMENT", "") or "unknown"),
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(TRACER_NAME)
        _status_ok = Status(StatusCode.OK)
        _status_error = StatusCode.ERROR
    except Exception as exc:  # noqa: BLE001
        _configured = True
        _tracer = None
        logger.error(
            "Tracing configuration FAILED (%s). Tracing stays OFF; the "
            "investigation path is unaffected.", exc,
        )
        return False

    _configured = True
    logger.info(
        "Tracing ENABLED | exporter=OTLP/http | endpoint=%s | service=%s",
        endpoint, getattr(settings, "OTEL_SERVICE_NAME", "aeam"),
    )
    return True


def tracing_enabled() -> bool:
    """``True`` when a real tracer is active and spans are being emitted."""
    return _tracer is not None


def reset_tracing_for_tests() -> None:
    """Reset module state so a test can reconfigure tracing. Test-only."""
    global _tracer, _configured, _status_ok, _status_error
    _tracer = None
    _configured = False
    _status_ok = None
    _status_error = None


@contextmanager
def investigation_span(
    name: str,
    incident_id: str | None = None,
    **attributes: Any,
) -> Iterator[Any]:
    """
    Emit one span for a stage of the investigation path.

    Nesting is automatic: a span opened inside another span's ``with`` block
    becomes its child, so one ``handle_event`` produces a single connected
    trace spanning decision → evidence stages → planning → action.

    A no-op (yielding ``None``) whenever tracing is disabled or the SDK is
    absent, so call sites need no conditional of their own.

    Args:
        name:        Span name, e.g. ``"decision"``, ``"evidence.rag"``.
        incident_id: The incident this stage belongs to. Recorded as
                     :data:`INCIDENT_ID_ATTRIBUTE` so the span joins to the
                     log lines carrying the same id.
        attributes:  Extra span attributes. ``None`` values are dropped
                     rather than exported as the string ``"None"``.

    Yields:
        The active span, or ``None`` when tracing is off.
    """
    if _tracer is None:
        yield None
        return

    try:
        span_cm = _tracer.start_as_current_span(name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("investigation_span | failed to start span %r: %s", name, exc)
        yield None
        return

    with span_cm as span:
        try:
            if incident_id:
                span.set_attribute(INCIDENT_ID_ATTRIBUTE, str(incident_id))
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(f"aeam.{key}", value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("investigation_span | attribute set failed: %s", exc)

        try:
            yield span
        except Exception as exc:
            # Record the failure on the span, then let it propagate — tracing
            # never swallows an application error.
            try:
                span.record_exception(exc)
                if _status_error is not None:
                    span.set_status(_status_error)
            except Exception:  # noqa: BLE001
                pass
            raise
        else:
            try:
                if _status_ok is not None:
                    span.set_status(_status_ok)
            except Exception:  # noqa: BLE001
                pass


def current_trace_id() -> str | None:
    """
    The active trace's id as a 32-char hex string, or ``None`` when no span
    is active (including whenever tracing is disabled).

    Used to stamp the trace id into the incident's ``audit_summary`` so an
    operator can jump from a persisted incident straight to its trace.
    """
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        context = span.get_span_context()
        if not context or not context.is_valid:
            return None
        return format(context.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return None
