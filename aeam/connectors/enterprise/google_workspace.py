"""
aeam/connectors/enterprise/google_workspace.py

Google Workspace (Drive) document connector (Phase F7).

Lists files in a Drive folder through the Drive v3 API and feeds them into the
existing ingestion pipeline.

Deliberately distinct from the existing ``SheetsConnector``, which is untouched
by this phase: that one reads a spreadsheet's rows as KPI series (COMPAT-4,
unchanged). This one ingests Drive files as knowledge documents. Two different
jobs against the same vendor, not merged — merging them would give one
connector two capabilities and break the uniform contract every other
connector honours.

Drive exposes both ``modifiedTime`` and a monotonic ``version``. The version is
the change signature, because Drive bumps ``modifiedTime`` for metadata-only
edits that leave content byte-identical.
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


class GoogleWorkspaceConnector(DocumentConnector):
    """Google Drive document connector."""

    kind = SourceKind.GOOGLE_WORKSPACE
    display_name = "Google Workspace (Drive)"
    required_config = ("folder_id",)
    default_secret_key = "GOOGLE_WORKSPACE_ACCESS_TOKEN"
    field_map = ListingFieldMap(
        external_id="id",
        title="name",
        content_type="mimeType",
        source_type="kind",
        source_url="webViewLink",
        source_timestamp="modifiedTime",
        source_version="version",
        # Drive's md5Checksum exists for binary uploads but not for native
        # Docs/Sheets, so it is not a dependable signature across a real folder.
        # Left unmapped; `version` carries change detection for every file type.
        content_hash=None,
        size_bytes="size",
        semantic_type=None,
    )

    def _build_client(self, secret: str) -> Any:
        folder = str(self._config["folder_id"]).strip()
        api = str(
            self._config.get("api_base_url") or "https://www.googleapis.com/drive/v3"
        ).rstrip("/")
        fields = "files(id,name,mimeType,modifiedTime,version,webViewLink,size,kind)"
        return RestDocumentClient(
            list_url=f"{api}/files?q=%27{folder}%27+in+parents&fields={fields}",
            download_url=f"{api}/files/{{external_id}}?alt=media",
            headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            items_key="files",
            since_param="q",
            timeout=float(self._config.get("timeout_seconds") or 30.0),
        )

    def _filename_for(self, title: str, item: dict[str, Any]) -> str:
        """Drive names often lack an extension for native Docs.

        The export the download URL yields is plain text, so ``.txt`` describes
        what the bytes actually are.
        """
        return ensure_extension(title, "txt")
