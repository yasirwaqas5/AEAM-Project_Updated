"""
aeam/connectors/enterprise/confluence.py

Confluence document connector (Phase F7).

Lists pages in a Confluence space through the Cloud REST API and feeds them
into the existing ingestion pipeline.

Confluence exposes a monotonic integer page version, which is a stronger change
signal than a timestamp: a page saved without edits does not bump it. This
connector reports that version as its change signature, so an untouched page is
skipped from the listing alone.

Page titles are prose ("Runbook: DB Latency"), not filenames, and the existing
ingestion validator derives a document's format from its filename extension.
Titles are therefore given an ``.html`` extension — Confluence storage format
really is markup, so that is a statement of fact rather than a convenience.
"""

from __future__ import annotations

from typing import Any

from aeam.connectors.enterprise.documents import (
    DocumentConnector,
    ListingFieldMap,
    RestDocumentClient,
    ensure_extension,
)
from aeam.registry.models import SourceKind


class ConfluenceConnector(DocumentConnector):
    """Confluence space connector."""

    kind = SourceKind.CONFLUENCE
    display_name = "Confluence"
    required_config = ("base_url", "space_key")
    default_secret_key = "CONFLUENCE_API_TOKEN"
    #: Confluence returns ``version`` as a nested object, which the transport
    #: flattens to ``versionNumber`` so this map stays declarative. No ETag is
    #: exposed, so ``content_hash`` is left unmapped — the version carries
    #: change detection, and pretending an ETag existed would be a guess.
    field_map = ListingFieldMap(
        external_id="id",
        title="title",
        content_type=None,
        source_type="type",
        source_url="_webui",
        source_timestamp="when",
        source_version="versionNumber",
        content_hash=None,
        size_bytes=None,
        semantic_type="aeamSemanticType",
    )

    def _build_client(self, secret: str) -> Any:
        base = str(self._config["base_url"]).rstrip("/")
        space = str(self._config["space_key"]).strip()
        return RestDocumentClient(
            list_url=f"{base}/rest/api/content?spaceKey={space}&expand=version,history",
            download_url=f"{base}/rest/api/content/{{external_id}}?expand=body.storage",
            headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            items_key="results",
            since_param="cql",
            timeout=float(self._config.get("timeout_seconds") or 30.0),
        )

    def _filename_for(self, title: str, item: dict[str, Any]) -> str:
        return ensure_extension(title, "html")
