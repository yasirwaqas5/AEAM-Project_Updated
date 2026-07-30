"""
aeam/connectors/enterprise/mock.py

Deterministic mock upstream clients (Phase F7).

Gating CI never calls SharePoint, Confluence, GitHub, Google Workspace, SAP,
Salesforce, Snowflake, or BigQuery. It cannot: those calls need a tenant, a
licence, and a credential, and a test suite that depends on any of them is a
test suite that fails for reasons unrelated to the code. This module is what
the connectors talk to instead.

These are not stubs that return ``True``. Each one models the *shape* of its
upstream API — the field names, the id formats, the change semantics — well
enough that a connector's real translation logic runs against it. That is what
makes the shared contract suite meaningful: it exercises the connector, and
only the transport is fake.

Two properties every mock guarantees, because the tests depend on them:

* **deterministic** — the same fixture yields the same ids, bytes, and hashes
  on every call, so an incremental-sync assertion is reproducible;
* **mutable on demand** — ``mutate()`` changes an artifact's content and
  bumps its version/timestamp, which is how a test proves that a changed
  artifact is re-ingested while its unchanged neighbours are skipped.

The same clients also back ``CONNECTOR_MOCK_MODE``, so an operator can stand
the framework up and watch a real sync run end-to-end before any credential
exists. That mode is honest about itself: the connector's health reports
``client_mode: "injected"`` and the console labels it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _deterministic_bytes(seed: str, body: str) -> bytes:
    """Stable bytes for a fixture artifact.

    Seeded by id so two artifacts never collide, and stable across runs so a
    content hash computed today equals the one computed tomorrow — which is
    what lets a duplicate-sync test assert "no work was done".
    """
    return f"# {seed}\n\n{body}\n".encode("utf-8")


@dataclass
class MockArtifact:
    """One artifact in a mock upstream system."""

    external_id: str
    title: str
    body: str
    content_type: str = "text/markdown"
    source_type: str = "page"
    url: str | None = None
    timestamp: str | None = "2026-07-01T00:00:00+00:00"
    version: str | None = "1"
    #: When True the mock exposes no hash/version/timestamp at all, forcing the
    #: sync engine down its download-and-compare path. A real upstream like a
    #: plain WebDAV share behaves this way, and the engine must stay correct
    #: (just not free) for it.
    opaque: bool = False
    semantic_type: str | None = None

    def data(self) -> bytes:
        return _deterministic_bytes(self.external_id, self.body)

    def content_hash(self) -> str | None:
        if self.opaque:
            return None
        return hashlib.sha256(self.data()).hexdigest()


@dataclass(frozen=True)
class MockDialect:
    """
    How one real upstream system names the fields in its listing response.

    Written from each vendor's ACTUAL API shape, independently of the
    connector's ``ListingFieldMap``. That independence is what makes the
    contract suite a real test: if a connector's field map disagrees with its
    vendor's naming, the connector translates nothing and the suite fails. A
    mock that simply echoed each map's keys back would be tautological.

    A concept the vendor does not expose is ``None`` here, so the connector is
    also tested on handling a genuinely absent field rather than only on
    reading a present one.
    """

    id_key: str = "id"
    title_key: str = "name"
    content_type_key: str | None = "content_type"
    type_key: str | None = "type"
    url_key: str | None = "url"
    timestamp_key: str | None = "modified"
    version_key: str | None = "version"
    hash_key: str | None = "etag"
    size_key: str | None = "size"
    semantic_key: str | None = "semantic_type"


#: Per-vendor listing dialects, from each API's real response shape.
DIALECTS: dict[str, MockDialect] = {
    # Microsoft Graph driveItem: eTag changes with content, cTag is the
    # content-vs-metadata discriminator, mimeType comes from the `file` facet.
    "sharepoint": MockDialect(
        id_key="id", title_key="name", content_type_key="mimeType", type_key="itemType",
        url_key="webUrl", timestamp_key="lastModifiedDateTime", version_key="cTag",
        hash_key="eTag", size_key="size", semantic_key="aeamSemanticType",
    ),
    # Confluence Cloud content: a monotonic version, no ETag, no size, and
    # `title` rather than `name`.
    "confluence": MockDialect(
        id_key="id", title_key="title", content_type_key=None, type_key="type",
        url_key="_webui", timestamp_key="when", version_key="versionNumber",
        hash_key=None, size_key=None, semantic_key="aeamSemanticType",
    ),
    # GitHub contents API: the blob sha is the content address; there is no
    # per-file timestamp and no MIME type, and the id is the repo path.
    "github": MockDialect(
        id_key="path", title_key="name", content_type_key=None, type_key="type",
        url_key="html_url", timestamp_key=None, version_key="sha",
        hash_key="sha", size_key="size", semantic_key=None,
    ),
    # Drive v3 files: monotonic version, modifiedTime, no dependable checksum
    # across native and binary files.
    "google_workspace": MockDialect(
        id_key="id", title_key="name", content_type_key="mimeType", type_key="kind",
        url_key="webViewLink", timestamp_key="modifiedTime", version_key="version",
        hash_key=None, size_key="size", semantic_key=None,
    ),
}


class MockDocumentClient:
    """
    A mock document-yielding upstream system.

    Serves SharePoint, Confluence, GitHub, and Google Workspace. Their listing
    APIs differ in field names — modelled by :class:`MockDialect` — but the
    transport shape ("list things, then download one") is identical, so one
    mock plus a dialect is honest reuse rather than a shortcut.

    Args:
        artifacts: Fixture corpus.
        dialect:   Which vendor's field naming to emit. ``None`` uses the
                   generic naming, which no real connector reads — so a test
                   that forgets to pass a dialect fails loudly rather than
                   passing against invented field names.
    """

    def __init__(
        self,
        artifacts: list[MockArtifact] | None = None,
        dialect: MockDialect | str | None = None,
    ) -> None:
        self._artifacts: dict[str, MockArtifact] = {
            artifact.external_id: artifact for artifact in (artifacts or [])
        }
        self._dialect: MockDialect = (
            DIALECTS[dialect] if isinstance(dialect, str)
            else (dialect or MockDialect())
        )
        #: Call counters. Tests assert on these to prove incremental sync
        #: actually avoided work rather than merely producing the right answer.
        self.list_calls: int = 0
        self.fetch_calls: int = 0
        self.fetched_ids: list[str] = []
        #: When set, every call raises — the failure-isolation lever.
        self.fail_with: Exception | None = None

    # -- fixture control -------------------------------------------------

    def add(self, artifact: MockArtifact) -> "MockDocumentClient":
        self._artifacts[artifact.external_id] = artifact
        return self

    def mutate(self, external_id: str, body: str, version: str | None = None) -> None:
        """Change an artifact's content upstream, as an editor would.

        Bumps the version so a connector relying on version-based change
        detection sees it, and leaves the timestamp for the caller to control
        when a test needs timestamp-only semantics.
        """
        artifact = self._artifacts[external_id]
        artifact.body = body
        artifact.version = version or str(int(artifact.version or "1") + 1)

    def remove(self, external_id: str) -> None:
        self._artifacts.pop(external_id, None)

    # -- transport -------------------------------------------------------

    def list_items(self, since: str | None = None) -> list[dict[str, Any]]:
        """List artifacts in this dialect's field naming.

        ``since`` filtering is modelled only when the dialect actually exposes a
        timestamp, because that is the truth: GitHub's contents API cannot
        filter by modification time, and pretending it could would let a
        connector's incremental behaviour look better in tests than in
        production.
        """
        self.list_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        d = self._dialect
        items: list[dict[str, Any]] = []
        for artifact in sorted(self._artifacts.values(), key=lambda a: a.external_id):
            if (
                since is not None
                and d.timestamp_key is not None
                and artifact.timestamp is not None
                and not artifact.opaque
                and artifact.timestamp <= since
            ):
                continue

            item: dict[str, Any] = {
                d.id_key: artifact.external_id,
                d.title_key: artifact.title,
            }

            def put(key: str | None, value: Any) -> None:
                # A concept this vendor does not expose is simply absent from
                # the response, which is what the connector must cope with.
                if key is not None and value is not None:
                    item[key] = value

            put(d.content_type_key, artifact.content_type)
            put(d.type_key, artifact.source_type)
            put(d.url_key, artifact.url or f"https://mock.invalid/{artifact.external_id}")
            put(d.timestamp_key, None if artifact.opaque else artifact.timestamp)
            put(d.version_key, None if artifact.opaque else artifact.version)
            put(d.hash_key, artifact.content_hash())
            put(d.size_key, len(artifact.data()))
            put(d.semantic_key, artifact.semantic_type)
            items.append(item)
        return items

    def download(self, external_id: str) -> bytes:
        self.fetch_calls += 1
        self.fetched_ids.append(external_id)
        if self.fail_with is not None:
            raise self.fail_with
        artifact = self._artifacts.get(external_id)
        if artifact is None:
            raise KeyError(f"mock upstream has no artifact {external_id!r}")
        return artifact.data()

    def ping(self) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        return True

    def __repr__(self) -> str:
        return (
            f"MockDocumentClient(artifacts={len(self._artifacts)}, "
            f"dialect={self._dialect.id_key}/{self._dialect.title_key})"
        )


class MockMetricsClient:
    """
    A mock metrics-yielding upstream system.

    Serves SAP, Salesforce, Snowflake, and BigQuery. All four are queried the
    same way from AEAM's perspective — "run this named query/report, give me
    rows" — so the mock models exactly that, and each connector translates its
    own request/response naming on top.

    Rows come back in the shape ``MonitorAgent._extract_series`` already
    consumes (a date-ish column plus metric columns), because that is the whole
    point: a metrics connector is an ordinary ``CompositeKPISource`` member and
    ``MonitorAgent`` never learns it exists.
    """

    def __init__(self, rows_by_selector: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = dict(rows_by_selector or {})
        self.query_calls: int = 0
        self.queried_selectors: list[str] = []
        self.fail_with: Exception | None = None

    def add_rows(self, selector: str, rows: list[dict[str, Any]]) -> "MockMetricsClient":
        self._rows[selector] = list(rows)
        return self

    def query(self, selector: str) -> list[dict[str, Any]]:
        self.query_calls += 1
        self.queried_selectors.append(selector)
        if self.fail_with is not None:
            raise self.fail_with
        # An unknown selector returns no rows rather than raising: an upstream
        # report that does not exist is the same situation as one with no data,
        # and MonitorAgent already treats "no rows" as a no-op.
        return [dict(row) for row in self._rows.get(selector, [])]

    def ping(self) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        return True

    def __repr__(self) -> str:
        return f"MockMetricsClient(selectors={sorted(self._rows)})"


# ---------------------------------------------------------------------------
# Ready-made fixtures
# ---------------------------------------------------------------------------

def default_document_fixture(dialect: MockDialect | str | None = None) -> MockDocumentClient:
    """A small, realistic document corpus in ``dialect``'s field naming.

    Deliberately mixed: two artifacts expose a change signature and one is
    ``opaque``, so any suite using this fixture exercises BOTH of the sync
    engine's change-detection paths — signature comparison and
    download-and-hash — without having to construct the awkward case by hand.
    """
    return MockDocumentClient(
        [
            MockArtifact(
                external_id="doc-runbook",
                title="database-latency-runbook.md",
                body="When db_latency_ms exceeds 800ms, check the index on orders.created_at.",
                source_type="page",
                semantic_type="runbook",
            ),
            MockArtifact(
                external_id="doc-policy",
                title="escalation-policy.md",
                body="If sales drop by more than 30%, escalate to the regional director.",
                source_type="page",
                semantic_type="policy",
            ),
            MockArtifact(
                external_id="doc-opaque",
                title="legacy-notes.txt",
                body="Undated operational notes with no upstream version metadata.",
                content_type="text/plain",
                source_type="file",
                opaque=True,
            ),
        ],
        dialect=dialect,
    )


def default_metrics_fixture(selector: str) -> MockMetricsClient:
    """A short, chronologically-ordered metric series under ``selector``."""
    return MockMetricsClient({
        selector: [
            {"date": "2026-07-01", "revenue": 100.0, "orders": 12},
            {"date": "2026-07-02", "revenue": 104.0, "orders": 13},
            {"date": "2026-07-03", "revenue": 99.0, "orders": 11},
            {"date": "2026-07-04", "revenue": 41.0, "orders": 5},
        ]
    })
