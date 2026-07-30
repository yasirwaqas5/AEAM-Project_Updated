"""
aeam/connectors/enterprise/sharepoint.py

SharePoint document connector (Phase F7).

Lists drive items from a SharePoint/OneDrive document library through the
Microsoft Graph API and feeds them into the existing ingestion pipeline.

Graph is unusually cooperative for incremental sync: every ``driveItem``
carries an ``eTag`` that changes when content changes, plus a
``lastModifiedDateTime``. The eTag is what this connector reports as its change
signature, which means an unchanged file is recognised from the listing alone —
no download at all.
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


class SharePointConnector(DocumentConnector):
    """SharePoint document library connector."""

    kind = SourceKind.SHAREPOINT
    display_name = "SharePoint"
    #: ``site_url`` identifies the tenant/site; ``drive_id`` the library. Both
    #: are non-secret and both are required — without either, Graph has no
    #: addressable collection, and "not configured" is the honest state rather
    #: than a sync that fails halfway.
    required_config = ("site_url", "drive_id")
    default_secret_key = "SHAREPOINT_ACCESS_TOKEN"
    #: Graph's own field names. ``eTag`` is the change signature; ``cTag``
    #: distinguishes content changes from metadata changes and is kept as the
    #: version.
    field_map = ListingFieldMap(
        external_id="id",
        title="name",
        content_type="mimeType",
        source_type="itemType",
        source_url="webUrl",
        source_timestamp="lastModifiedDateTime",
        source_version="cTag",
        content_hash="eTag",
        size_bytes="size",
        semantic_type="aeamSemanticType",
    )

    def _build_client(self, secret: str) -> Any:
        drive = str(self._config["drive_id"]).strip()
        graph = str(
            self._config.get("graph_base_url") or "https://graph.microsoft.com/v1.0"
        ).rstrip("/")
        return RestDocumentClient(
            list_url=f"{graph}/drives/{drive}/root/children",
            download_url=f"{graph}/drives/{drive}/items/{{external_id}}/content",
            headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            items_key="value",
            # Graph filters with OData rather than a bare `since` parameter, so
            # the client sends what Graph understands instead of a parameter it
            # would silently ignore.
            since_param="$filter",
            timeout=float(self._config.get("timeout_seconds") or 30.0),
        )

    def _filename_for(self, title: str, item: dict[str, Any]) -> str:
        """SharePoint item names are real filenames, extension included.

        The fallback covers the folder-like items Graph occasionally returns
        without one; markdown is safe because extraction treats it as text.
        """
        return ensure_extension(title, "md")
