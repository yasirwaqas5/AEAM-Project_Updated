"""
aeam/connectors/enterprise/github.py

GitHub document connector (Phase F7).

Lists files under a configured repository path (docs/, runbooks/, …) through
the GitHub REST API and feeds them into the existing ingestion pipeline. Works
against github.com and GitHub Enterprise alike — the API host is
configuration, not code.

GitHub gives the strongest change signal of any connector here: every blob
carries its content ``sha``, which by definition changes if and only if the
content changes. No timestamps, no heuristics — an unchanged file is *provably*
unchanged from the listing alone.
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


class GitHubConnector(DocumentConnector):
    """GitHub repository document connector."""

    kind = SourceKind.GITHUB
    display_name = "GitHub"
    #: ``repository`` is ``owner/name``. ``path`` scopes which subtree is
    #: ingested — required rather than defaulted to the repo root, because
    #: ingesting an entire source tree as "knowledge documents" is almost never
    #: what an operator means, and a default that is usually wrong is worse
    #: than no default.
    required_config = ("repository", "path")
    default_secret_key = "GITHUB_TOKEN"
    #: The git blob sha IS the change signature — content-addressed by
    #: definition. GitHub's contents API exposes no per-file timestamp, and that
    #: absence is left honest rather than filled from the commit date, which
    #: would change for files the commit did not touch.
    field_map = ListingFieldMap(
        external_id="path",
        title="name",
        content_type=None,
        source_type="type",
        source_url="html_url",
        source_timestamp=None,
        source_version="sha",
        content_hash="sha",
        size_bytes="size",
        semantic_type=None,
    )

    def _build_client(self, secret: str) -> Any:
        repo = str(self._config["repository"]).strip("/")
        path = str(self._config["path"]).strip("/")
        api = str(self._config.get("api_base_url") or "https://api.github.com").rstrip("/")
        ref = str(self._config.get("ref") or "").strip()
        query = f"?ref={ref}" if ref else ""
        return RestDocumentClient(
            list_url=f"{api}/repos/{repo}/contents/{path}{query}",
            download_url=f"{api}/repos/{repo}/contents/{{external_id}}{query}",
            headers={
                "Authorization": f"Bearer {secret}",
                # The raw media type returns file bytes directly rather than a
                # base64 JSON envelope, so no decoding step is needed here.
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            items_key=None,
            # The contents API has no since-filter. Declared as None so the
            # connector does not send a parameter GitHub would ignore; the sync
            # engine's per-artifact sha comparison does the filtering instead,
            # at the cost of one listing call.
            since_param=None,
            timeout=float(self._config.get("timeout_seconds") or 30.0),
        )

    def _filename_for(self, title: str, item: dict[str, Any]) -> str:
        """Repository filenames already carry their real extension.

        The fallback covers extensionless files (LICENSE, CODEOWNERS); treating
        those as text is accurate rather than convenient.
        """
        return ensure_extension(title, "txt")
