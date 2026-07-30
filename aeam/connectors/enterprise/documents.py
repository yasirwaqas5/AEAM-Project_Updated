"""
aeam/connectors/enterprise/documents.py

Shared scaffolding for the four document-yielding connectors (Phase F7).

SharePoint, Confluence, GitHub, and Google Workspace differ in three ways and
three ways only, from AEAM's persp:

1. the endpoint they list from,
2. the field names in the listing response,
3. how the credential becomes an HTTP header.

Everything else — walking the listing, deciding what changed, downloading the
changed bytes, handing them to the existing ingestion pipeline — is identical.
So it lives here once. Each connector module supplies its three differences as
declarative data and adds nothing else.

That is what "connector-specific behavior belongs only inside connector
implementations" means in practice: the *behaviour* is per-connector, and it
is expressed as a field map and an auth style rather than as a fourth copy of
a listing loop. A fifth document connector is a ~30-line module.

The transport seam
------------------
Every connector talks to a *client object* with two methods
(``list_items(since)`` and ``download(external_id)``), never to ``requests``
directly. That is the seam that keeps gating CI offline: tests inject
:class:`~aeam.connectors.enterprise.mock.MockDocumentClient`, and the real
:class:`RestDocumentClient` below is only ever constructed when a credential
actually exists.

When no client can be built — no credential, no configuration — the connector
reports ``authenticated: false`` with the reason. It does not raise at
construction and it does not pretend: an unknown state reported as healthy is
the failure mode SEC-8 exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aeam.connectors.base import (
    ConnectorArtifactRef,
    ConnectorAuthError,
    ConnectorUnavailableError,
    EnterpriseConnector,
)
from aeam.registry.models import ConnectorCapability, SemanticDocType

logger = logging.getLogger(__name__)

#: Bound on how many items one listing call may return, so a connector against
#: a 200k-item library cannot materialise the whole thing in memory. The sync
#: engine applies its own per-run cap on top; this one protects the process
#: even if a connector is called directly.
DEFAULT_LIST_LIMIT: int = 1000


@dataclass(frozen=True)
class ListingFieldMap:
    """
    How one upstream system names the fields AEAM needs.

    Every attribute is the upstream's key for a concept
    :class:`~aeam.connectors.base.ConnectorArtifactRef` defines. A concept the
    upstream does not expose is mapped to ``None``, and the resulting ref field
    stays ``None`` — never a substituted plausible value, because a fabricated
    timestamp or version would make incremental sync silently wrong.
    """

    external_id: str = "id"
    title: str = "name"
    content_type: str | None = "content_type"
    source_type: str | None = "type"
    source_url: str | None = "url"
    source_timestamp: str | None = "modified"
    source_version: str | None = "version"
    content_hash: str | None = "etag"
    size_bytes: str | None = "size"
    semantic_type: str | None = "semantic_type"


class RestDocumentClient:
    """
    A minimal REST client for document connectors.

    Built on ``requests`` (already a dependency) and constructed ONLY when a
    credential has been resolved. It is never exercised in gating CI — the
    contract suite injects a mock instead — which is why it stays deliberately
    small: a large real client would be a large untested surface.

    Args:
        list_url:      Absolute URL that returns the item listing.
        download_url:  Template containing ``{external_id}``.
        headers:       Fully-formed request headers, credential included. Held
                       for the lifetime of the client and dropped by
                       :meth:`close`; never logged.
        items_key:     Key under which the listing response nests its array,
                       or ``None`` when the response *is* the array.
        since_param:   Query parameter name for server-side change filtering,
                       or ``None`` when upstream offers none.
        timeout:       Per-request timeout. A connector hanging on a wedged
                       upstream is the mechanism by which one connector could
                       starve everything else, so a timeout is mandatory
                       rather than advisory.
    """

    def __init__(
        self,
        list_url: str,
        download_url: str,
        headers: dict[str, str],
        items_key: str | None = None,
        since_param: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._list_url = list_url
        self._download_url = download_url
        self._headers = dict(headers)
        self._items_key = items_key
        self._since_param = since_param
        self._timeout = float(timeout)

    def list_items(self, since: str | None = None) -> list[dict[str, Any]]:
        import requests

        params: dict[str, Any] = {}
        if since and self._since_param:
            params[self._since_param] = since
        try:
            response = requests.get(
                self._list_url, headers=self._headers, params=params, timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            # The message may echo the credential back from an upstream error
            # body; the connector sanitises before anything durable happens.
            raise ConnectorUnavailableError(f"listing failed: {exc}") from exc

        items = payload.get(self._items_key, []) if self._items_key else payload
        if not isinstance(items, list):
            raise ConnectorUnavailableError(
                f"listing response was {type(items).__name__}, expected a list."
            )
        return items[:DEFAULT_LIST_LIMIT]

    def download(self, external_id: str) -> bytes:
        import requests

        try:
            response = requests.get(
                self._download_url.format(external_id=external_id),
                headers=self._headers, timeout=self._timeout,
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:  # noqa: BLE001
            raise ConnectorUnavailableError(f"download failed: {exc}") from exc

    def ping(self) -> bool:
        self.list_items()
        return True

    def close(self) -> None:
        self._headers.clear()

    def __repr__(self) -> str:
        return "RestDocumentClient()"


class DocumentConnector(EnterpriseConnector):
    """
    Base for every document-yielding connector.

    Subclasses declare ``kind``, ``display_name``, ``required_config``, a
    :class:`ListingFieldMap`, and how to build a real client. They implement no
    listing, change-detection, download, or ingestion logic — all of that is
    here, once.
    """

    capability = ConnectorCapability.DOCUMENTS
    #: Upstream's field naming. Subclasses override.
    field_map: ListingFieldMap = ListingFieldMap()
    #: Secret key checked when ``sources.secret_ref`` is unset, so a connector
    #: works from a conventional environment variable without per-source
    #: configuration.
    default_secret_key: str = ""

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """
        Resolve the credential and verify the upstream is reachable.

        An injected client (tests, mock mode) is trusted as already
        authenticated — that is the point of injecting it — but is still
        pinged, so "authenticated" always means a call actually succeeded.
        """
        configured, reason = self.validate_config()
        if not configured:
            self._authenticated = False
            self._auth_error = reason
            return False

        if self._client is None:
            secret = self._resolve_secret() or self._resolve_secret(self.default_secret_key)
            if not secret:
                self._authenticated = False
                self._auth_error = (
                    f"No credential available. Set {self._secret_ref or self.default_secret_key!r} "
                    "so SecretManager can resolve it, or inject a client."
                )
                return False
            try:
                self._client = self._build_client(secret)
            except Exception as exc:  # noqa: BLE001
                self._authenticated = False
                self._auth_error = self.sanitize(f"client construction failed: {exc}")
                return False

        try:
            self._client.ping()
        except Exception as exc:  # noqa: BLE001
            self._authenticated = False
            self._auth_error = self.sanitize(f"upstream rejected the connection: {exc}")
            return False

        self._authenticated = True
        self._auth_error = None
        return True

    def _build_client(self, secret: str) -> Any:
        """Construct the real upstream client from a resolved credential.

        Subclasses override to supply their endpoints and auth header shape.
        Never called when a client was injected.
        """
        raise ConnectorAuthError(
            f"{type(self).__name__} has no real client implementation; inject one "
            "or run in mock mode."
        )

    # ------------------------------------------------------------------
    # Capability: DOCUMENTS
    # ------------------------------------------------------------------

    def list_artifacts(self, since: str | None = None) -> list[ConnectorArtifactRef]:
        """Translate the upstream listing into refs, dropping nothing silently.

        An item missing the id or title AEAM needs is skipped with a warning
        rather than given a generated id: a synthesised id would be unstable
        across syncs, and unstable ids re-ingest the same artifact forever.
        """
        if self._client is None:
            raise ConnectorAuthError(
                "list_artifacts called before authenticate() established a client."
            )
        items = self._client.list_items(since=since) or []
        refs: list[ConnectorArtifactRef] = []
        for item in items:
            ref = self._to_ref(item)
            if ref is None:
                continue
            refs.append(ref)
        logger.info(
            "connector %s | listed %d artifact(s) | since=%s",
            self.kind, len(refs), since,
        )
        return refs

    def fetch_artifact(self, ref: ConnectorArtifactRef) -> bytes:
        if self._client is None:
            raise ConnectorAuthError(
                "fetch_artifact called before authenticate() established a client."
            )
        data = self._client.download(ref.external_id)
        if not isinstance(data, (bytes, bytearray)):
            raise ConnectorUnavailableError(
                f"upstream returned {type(data).__name__} for {ref.external_id!r}, "
                "expected bytes."
            )
        return bytes(data)

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    def _to_ref(self, item: dict[str, Any]) -> ConnectorArtifactRef | None:
        """One upstream item → one :class:`ConnectorArtifactRef`, or ``None``.

        The only place upstream field names are read. Everything downstream —
        the sync engine, the provenance row, the console — sees the uniform
        ref, which is why none of them contains a per-connector branch.
        """
        fm = self.field_map
        external_id = str(item.get(fm.external_id) or "").strip()
        title = str(item.get(fm.title) or "").strip()
        if not external_id or not title:
            logger.warning(
                "connector %s | skipping item with no stable id/title: keys=%s",
                self.kind, sorted(item),
            )
            return None

        declared_type = self._pick(item, fm.semantic_type)
        semantic_type = (
            str(declared_type).strip().lower()
            if declared_type and str(declared_type).strip().lower() in SemanticDocType.ALL
            else None
        )
        return ConnectorArtifactRef(
            external_id=external_id,
            title=self._filename_for(title, item),
            content_type=self._pick(item, fm.content_type),
            source_type=self._pick(item, fm.source_type),
            source_url=self._pick(item, fm.source_url),
            source_timestamp=self._pick(item, fm.source_timestamp),
            source_version=self._as_str(self._pick(item, fm.source_version)),
            content_hash=self._as_str(self._pick(item, fm.content_hash)),
            semantic_type=semantic_type,
            size_bytes=self._as_int(self._pick(item, fm.size_bytes)),
            metadata={k: v for k, v in item.items() if isinstance(v, (str, int, float, bool))},
        )

    def _filename_for(self, title: str, item: dict[str, Any]) -> str:
        """The name the ingestion validator will see.

        Overridable because upstream titles are not always filenames: a
        Confluence page is "Runbook: DB Latency" with no extension, and the
        existing validator derives the format category from an extension. Each
        connector knows what its artifacts really are, so each decides.
        """
        return title

    @staticmethod
    def _pick(item: dict[str, Any], key: str | None) -> Any:
        """Read a mapped field, or ``None`` when the concept is unmapped.

        ``None`` for the key means "upstream does not expose this", and the
        result is ``None`` — the honest representation, never a default.
        """
        if key is None:
            return None
        value = item.get(key)
        return value if value not in ("", None) else None

    @staticmethod
    def _as_str(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def close(self) -> None:
        client_close = getattr(self._client, "close", None)
        if callable(client_close):
            try:
                client_close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("connector %s | client close failed: %s", self.kind, exc)
        super().close()


def ensure_extension(title: str, default_extension: str) -> str:
    """
    Give ``title`` a usable extension when it has none.

    Needed because the existing ingestion validator derives a document's format
    category from its filename, and several upstream systems name artifacts
    without one (a Confluence page title, a Salesforce report name). Appending
    the extension the connector *knows* the content actually has is a
    statement of fact, not a guess — a Confluence page really is markup, and a
    Google Doc export really is the format it was exported as.

    A title that already ends in a plausible extension is returned unchanged,
    so ``notes.txt`` never becomes ``notes.txt.md``.
    """
    cleaned = (title or "untitled").strip() or "untitled"
    tail = cleaned.rsplit(".", 1)
    if len(tail) == 2 and 1 <= len(tail[1]) <= 5 and tail[1].isalnum():
        return cleaned
    return f"{cleaned}.{default_extension.lstrip('.')}"
