"""
aeam/connectors/base.py

The Enterprise Connector contract (Phase F7).

One abstraction, one contract, eight implementations. Everything
connector-specific lives *inside* a connector; nothing connector-specific
lives anywhere else in the platform. There is no per-connector branch in the
sync engine, the ingestion pipeline, the KPI path, the API, or the console —
each of those talks to :class:`EnterpriseConnector` and nothing else.

The pattern is not new here. ``BlobStore`` (E4) already abstracts storage
backends behind one ABC, and ``CompositeKPISource`` (B1.5.3) already fans a
single ``fetch_rows(selector)`` out to members whose kinds it never learns.
This module generalizes that same seam so a connector is just another member.

The two capabilities, and why there are only two
------------------------------------------------
A connector declares :class:`~aeam.registry.models.ConnectorCapability`:

* ``DOCUMENTS`` — it yields artifacts that become retrievable documents. They
  reach the **existing** ingestion pipeline through
  :class:`~aeam.ingestion.submission.IngestionSubmitter`, exactly as an
  uploaded file does, and are therefore indistinguishable afterwards.
* ``METRICS`` — it yields rows that become KPI series. They reach the
  **existing** detection path through ``CompositeKPISource``, which
  ``MonitorAgent`` already consumes.

Those are the only two places content can enter AEAM, so those are the only
two capabilities. A third would mean a second ingestion or detection path,
which ENG-6 forbids.

Both capability methods are CONCRETE on this base with honest defaults:
``list_artifacts`` returns nothing and ``fetch_rows`` returns nothing when the
declared capability does not include them. That is deliberate — it keeps the
contract uniform (every connector answers every method, so the shared
contract suite can call all of them on all of them) without forcing a
metrics connector to pretend it has documents.

Credentials (SEC-5)
-------------------
A connector never receives, stores, or logs a credential value. It receives a
:class:`~aeam.integrations.secret_manager.SecretManager` and a
``secret_ref`` — a NAME — and resolves the value at the moment it
authenticates. :meth:`EnterpriseConnector.health` and
:meth:`describe` are built from configuration and outcomes only, and
:func:`sanitize_error` scrubs any resolved secret out of an exception message
before it can reach a log, a persisted run record, or an API response.

Failure isolation
-----------------
Every method on a connector is allowed to fail. What is not allowed is for
that failure to reach anything else — so :class:`ConnectorError` is the one
type the sync engine catches, connectors raise it (or any exception; the
engine catches broadly), and the engine isolates each connector's run.
Nothing a connector does can block another connector, ingestion, KPI
collection, retrieval, or startup.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from aeam.registry.models import ConnectorCapability

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """
    A connector-level failure.

    Carries a ``reason`` code alongside the message so the sync engine and the
    health surface can distinguish "not configured" from "authentication
    failed" from "upstream unavailable" without parsing prose.

    The message is expected to be credential-free; :func:`sanitize_error` is
    applied on the way out regardless, because a connector author forgetting
    once must not leak a token (SEC-5).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ConnectorNotConfiguredError(ConnectorError):
    """Required non-secret configuration is missing or invalid."""

    def __init__(self, detail: str) -> None:
        super().__init__("not_configured", detail)


class ConnectorAuthError(ConnectorError):
    """Credentials are absent, or upstream rejected them."""

    def __init__(self, detail: str) -> None:
        super().__init__("authentication_failed", detail)


class ConnectorUnavailableError(ConnectorError):
    """The upstream system could not be reached or returned an error."""

    def __init__(self, detail: str) -> None:
        super().__init__("upstream_unavailable", detail)


#: Replacement written over any secret value found in an error message.
_REDACTED: str = "***redacted***"

#: Minimum length a resolved secret must have before it is worth scrubbing.
#: A one- or two-character "secret" would match everywhere and redact the
#: whole message into uselessness; a real credential is never that short.
_MIN_SCRUB_LENGTH: int = 6


def sanitize_error(message: Any, secrets: list[str] | None = None) -> str:
    """
    Return ``message`` as a string with any supplied secret value removed.

    Called on every path where a failure becomes durable or visible — a log
    line, a persisted ``connector_sync_runs.error``, an API response, the
    health surface. Upstream systems routinely echo a token back in an error
    body ("invalid bearer eyJ..."), so scrubbing at the boundary is the only
    reliable place: by the time a message reaches a log it is too late.

    Args:
        message: The exception or text to sanitise.
        secrets: Resolved secret VALUES to remove. Never their names — a name
                 like ``GITHUB_TOKEN`` is not sensitive and removing it would
                 make errors harder to diagnose for no benefit.

    Returns:
        The sanitised string. Always a string, never ``None``, so a caller can
        persist it without a second guard.
    """
    text = str(message) if message is not None else ""
    for secret in secrets or []:
        if isinstance(secret, str) and len(secret) >= _MIN_SCRUB_LENGTH:
            text = text.replace(secret, _REDACTED)
    return text


@dataclass
class ConnectorArtifactRef:
    """
    One upstream artifact as the connector describes it, BEFORE it is fetched.

    This is the incremental-sync unit. The sync engine compares it against the
    recorded ``connector_artifacts`` row and only calls
    :meth:`EnterpriseConnector.fetch_artifact` when something actually
    changed — so listing is cheap and downloading is rare.

    Every field except ``external_id`` and ``title`` is optional, and an
    absent field means the upstream system does not expose it. A connector
    must leave it ``None`` rather than substituting something plausible: a
    fabricated ``source_timestamp`` would make incremental sync silently wrong
    (it would either re-ingest unchanged content forever or skip changed
    content), and a fabricated ``source_url`` would send an operator to a page
    that does not exist.

    Attributes:
        external_id:      The upstream system's own stable identifier. The
                          natural key sync state is recorded under, so it must
                          be stable across syncs for the same artifact.
        title:            Human-facing name. Also used as the ingestion
                          filename, so it should carry a usable extension when
                          the upstream artifact has one — the existing
                          validator derives the format category from it.
        content_type:     MIME type, when known.
        source_type:      What upstream calls this thing ("page", "file",
                          "blob"). Recorded verbatim, never mapped.
        source_url:       A URL an operator can open, when one exists.
        source_timestamp: Upstream's own last-modified time, when exposed.
        source_version:   Upstream's own revision id, when exposed.
        content_hash:     A hash upstream can give cheaply (an ETag, a git
                          blob sha). When present, the sync engine can decide
                          "unchanged" WITHOUT downloading — the single
                          biggest saving incremental sync offers.
        semantic_type:    Declared E12 semantic doc type, when upstream
                          metadata genuinely determines one. ``None`` is the
                          honest default; guessing would grant retrieval's
                          authoritative-source bonus to content that never
                          earned it.
        size_bytes:       Upstream-reported size, when exposed.
        metadata:         Any extra upstream fields worth retaining. Stored on
                          the provenance row's ``extra``; never used to derive
                          a decision.
    """

    external_id: str
    title: str
    content_type: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    source_timestamp: str | None = None
    source_version: str | None = None
    content_hash: str | None = None
    semantic_type: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def change_signature(self) -> str | None:
        """
        The cheapest available signature of this artifact's current state, or
        ``None`` when upstream exposes none.

        Preference order is deliberate: an upstream content hash is
        authoritative, a version id is nearly so, and a timestamp is the
        weakest of the three (clock skew, touch-without-change). ``None``
        means the connector cannot tell whether this artifact changed without
        downloading it, which the sync engine handles by downloading and
        comparing the real hash — correct, just not free.
        """
        if self.content_hash:
            return f"hash:{self.content_hash}"
        if self.source_version:
            return f"version:{self.source_version}"
        if self.source_timestamp:
            return f"timestamp:{self.source_timestamp}"
        return None


def content_hash_of(data: bytes) -> str:
    """
    SHA-256 of fetched bytes, in the same form ``BlobStore`` records.

    Used as the fallback change signal when upstream exposes no hash, version,
    or timestamp: the engine downloads once, hashes, and compares against the
    stored hash. Identical bytes then short-circuit the rest of the pipeline
    through the existing content-addressed dedup, so even this fallback never
    produces a duplicate document or embedding.
    """
    return hashlib.sha256(data).hexdigest()


class EnterpriseConnector(ABC):
    """
    The contract every connector implements — the whole contract, and the only
    contract.

    Args:
        source_id:      The ``sources`` row this connector instance serves.
                        Its provenance, sync state, and run history are all
                        keyed to this.
        config:         Non-secret connection parameters from
                        ``sources.config``. Never contains a credential.
        secret_manager: The shared
                        :class:`~aeam.integrations.secret_manager.SecretManager`.
                        The ONLY way a connector obtains a credential (SEC-5).
        secret_ref:     The NAME of the secret to resolve — from
                        ``sources.secret_ref``. A name, never a value.
        client:         Optional injected upstream client. This is the seam
                        that keeps gating CI offline: tests (and an operator
                        running in mock mode) pass a deterministic in-repo
                        mock client, and no live third-party call is ever
                        made. When ``None``, the connector builds its real
                        client — and if it cannot, it reports
                        ``configured``/``authenticated`` honestly as false
                        rather than pretending.

    Raises:
        ValueError: If ``source_id`` is blank.
    """

    #: The ``SourceKind`` this connector serves. Subclasses MUST set it.
    kind: str = ""
    #: The ``ConnectorCapability`` this connector declares. Subclasses MUST
    #: set it. The sync engine dispatches on this, not on the class.
    capability: str = ""
    #: Operator-facing name, used in health output and the console.
    display_name: str = ""
    #: ``sources.config`` keys this connector cannot work without. Checked by
    #: :meth:`validate_config`, so "not configured" is a fact rather than a
    #: failure discovered mid-sync.
    required_config: tuple[str, ...] = ()

    def __init__(
        self,
        source_id: str,
        config: dict[str, Any] | None = None,
        secret_manager: Any = None,
        secret_ref: str | None = None,
        client: Any = None,
    ) -> None:
        if not source_id or not str(source_id).strip():
            raise ValueError("source_id must not be blank.")
        if not self.kind:
            raise ValueError(f"{type(self).__name__} must declare a `kind`.")
        if self.capability not in ConnectorCapability.ALL:
            raise ValueError(
                f"{type(self).__name__} must declare a capability from "
                f"{sorted(ConnectorCapability.ALL)}. Got: {self.capability!r}."
            )
        self._source_id = str(source_id).strip()
        self._config = dict(config or {})
        self._secret_manager = secret_manager
        self._secret_ref = secret_ref
        self._client = client
        #: Resolved secret values seen during this instance's lifetime, held
        #: ONLY so :func:`sanitize_error` can scrub them out of messages.
        #: Never logged, never persisted, never returned — and cleared by
        #: :meth:`close`.
        self._resolved_secrets: list[str] = []
        self._authenticated: bool = False
        self._auth_error: str | None = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def config(self) -> dict[str, Any]:
        """A COPY of the non-secret configuration.

        A copy rather than the live dict so a caller inspecting configuration
        cannot mutate what the connector will use on its next call.
        """
        return dict(self._config)

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def validate_config(self) -> tuple[bool, str | None]:
        """
        Whether this connector has the non-secret configuration it needs.

        Returns ``(True, None)`` when configured, else ``(False, reason)``.
        Returning a reason rather than raising is deliberate: an unconfigured
        connector is a normal state for a fresh deployment, not an error, and
        the health surface needs to say WHICH key is missing.
        """
        missing = [
            key for key in self.required_config
            if not str(self._config.get(key) or "").strip()
        ]
        if missing:
            return False, (
                f"Missing required configuration: {', '.join(sorted(missing))}. "
                f"Set these in the connector's sources.config."
            )
        return True, None

    @property
    def is_configured(self) -> bool:
        configured, _reason = self.validate_config()
        return configured

    # ------------------------------------------------------------------
    # Credentials (SEC-5)
    # ------------------------------------------------------------------

    def _resolve_secret(self, key: str | None = None) -> str | None:
        """
        Resolve a credential through ``SecretManager`` and nowhere else.

        The value is returned to the caller and remembered ONLY for scrubbing
        (see :attr:`_resolved_secrets`). It is never written to a log, a
        database row, a health payload, or an exception — the three explicit
        prohibitions this method exists to make structural.

        Returns ``None`` when no secret manager is wired or the secret is not
        set, which the caller turns into an honest "not authenticated" rather
        than a crash.
        """
        name = key or self._secret_ref
        if not name or self._secret_manager is None:
            return None
        try:
            value = self._secret_manager.get_secret(name)
        except Exception as exc:  # noqa: BLE001
            # Deliberately does not include the exception text: a
            # SecretManager failure message could echo the key's surroundings.
            logger.warning(
                "connector %s | secret resolution failed for key=%r (%s)",
                self.kind, name, type(exc).__name__,
            )
            return None
        if isinstance(value, str) and value:
            if value not in self._resolved_secrets:
                self._resolved_secrets.append(value)
            return value
        return None

    def sanitize(self, message: Any) -> str:
        """Scrub every secret this connector has resolved out of ``message``."""
        return sanitize_error(message, self._resolved_secrets)

    @abstractmethod
    def authenticate(self) -> bool:
        """
        Establish (or verify) access to the upstream system.

        Must resolve credentials via :meth:`_resolve_secret`, set
        ``self._authenticated``, and record a credential-free
        ``self._auth_error`` on failure.

        Returns:
            ``True`` when the connector can talk to upstream.

        Raises:
            ConnectorError: Implementations MAY raise instead of returning
                            ``False`` when the distinction matters (missing
                            config vs. rejected credentials). The sync engine
                            treats both identically — isolated failure — so
                            either is correct.
        """

    # ------------------------------------------------------------------
    # Capability: DOCUMENTS
    # ------------------------------------------------------------------

    def list_artifacts(self, since: str | None = None) -> list[ConnectorArtifactRef]:
        """
        Upstream artifacts, optionally limited to those changed since
        ``since``.

        Base implementation returns an empty list, which is the truthful answer
        for a ``METRICS`` connector: it has no documents, and saying so is
        better than raising an error the sync engine would have to special-case.

        ``since`` is a hint, not a guarantee. A connector whose upstream
        supports server-side filtering should use it (that is where the real
        saving is); one whose upstream does not should list everything and let
        the sync engine's per-artifact comparison do the filtering. Either way
        the result is correct — the difference is only how much was
        transferred.
        """
        return []

    def fetch_artifact(self, ref: ConnectorArtifactRef) -> bytes:
        """
        Download one artifact's bytes.

        Called ONLY for artifacts the sync engine has already decided are new
        or changed, so it is the expensive operation that incremental sync
        exists to avoid.

        Base implementation raises, because reaching it means a ``METRICS``
        connector was asked for a document — a bug in the caller rather than a
        condition to degrade around.

        Raises:
            ConnectorError: On any upstream failure.
        """
        raise ConnectorError(
            "capability_mismatch",
            f"{type(self).__name__} declares capability {self.capability!r} and "
            "does not fetch document artifacts.",
        )

    # ------------------------------------------------------------------
    # Capability: METRICS
    # ------------------------------------------------------------------

    def fetch_rows(self, selector: str) -> list[dict[str, Any]]:
        """
        KPI rows for ``selector`` — the EXISTING ``KPIRowSource`` protocol.

        Signature-compatible with ``SheetsConnector.fetch_rows`` and
        ``DatasetKPISource.fetch_rows`` on purpose: a metrics connector is
        added to ``CompositeKPISource`` as an ordinary member, and
        ``MonitorAgent`` never learns it exists. No second detector, no second
        KPI path.

        Base implementation returns an empty list — the truthful answer for a
        ``DOCUMENTS`` connector, and the same "no rows" case
        ``MonitorAgent._run_cycle`` already handles as a no-op.

        Must NEVER raise: this runs on the monitoring hot path, where an
        exception would take down KPI collection for every other source.
        Implementations catch their own failures and return ``[]``.
        """
        return []

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """
        Static, credential-free description of this connector.

        Safe to log, persist, and return from an API: it contains the kind,
        capability, configuration KEYS (not values), and the secret's NAME
        (not its value).
        """
        configured, reason = self.validate_config()
        return {
            "source_id": self._source_id,
            "kind": self.kind,
            "capability": self.capability,
            "display_name": self.display_name or self.kind,
            "configured": configured,
            "configuration_reason": reason,
            # KEYS only. The values may contain a hostname or a project id,
            # which are not secrets — but a config dict is exactly where an
            # operator eventually pastes a token by mistake, so it is never
            # echoed back.
            "config_keys": sorted(self._config),
            "required_config": sorted(self.required_config),
            "secret_ref": self._secret_ref,
            "client_mode": "injected" if self._client is not None else "default",
        }

    def health(self) -> dict[str, Any]:
        """
        The connector's own view of its state.

        Reports only what it can observe. ``authenticated`` is ``False`` until
        :meth:`authenticate` has actually succeeded — never optimistically
        true, because an unknown state reported as healthy is the failure mode
        SEC-8 exists to prevent. Sync history and staleness are added by
        :class:`~aeam.connectors.health.ConnectorHealthReporter`, which reads
        the persisted run ledger; a connector cannot know its own history.
        """
        return {
            **self.describe(),
            "authenticated": self._authenticated,
            "auth_error": self._auth_error,
        }

    def close(self) -> None:
        """
        Release upstream resources and forget resolved credentials.

        Clearing :attr:`_resolved_secrets` is the point: a connector that
        finished its work should not still be holding a plaintext token in
        memory. Overriding subclasses must call ``super().close()``.
        """
        self._resolved_secrets.clear()
        self._authenticated = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(kind={self.kind!r}, capability={self.capability!r}, "
            f"source_id={self._source_id!r})"
        )
