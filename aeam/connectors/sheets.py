"""
aeam/connectors/sheets.py

Google Sheets data-access connector for the AEAM system.

Lightweight adapter that abstracts Google Sheets read/write operations.
Supports KPI data retrieval and log export workflows.

Rules:
- No business logic.
- No anomaly detection.
- No LLM calls.
- No orchestration logic.
- Connector failures NEVER stop AEAM startup.
- Missing credentials or missing libraries degrade gracefully to no-op mode.
- Credentials are never logged.

Optional runtime dependencies (lazy-imported):
    gspread
    google-auth (google.oauth2.service_account)

If either is unavailable the connector operates in disabled mode and
all methods return safe empty/False values.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("aeam.connectors.sheets")

# Google Sheets API OAuth2 scope — read/write access.
_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class SheetsConnector:
    """
    Google Sheets data-access connector.

    Provides synchronous read and write access to a configured Google Sheets
    spreadsheet using a service account. Designed as a thin adapter layer
    with no business logic.

    The connector degrades gracefully when:
    - ``settings.GOOGLE_SHEETS_SA_CREDENTIALS`` is absent or empty.
    - ``settings.SHEET_ID`` is absent or empty.
    - The ``gspread`` or ``google-auth`` libraries are not installed.
    - The Google Sheets API is unreachable.

    In all failure modes every public method returns a safe empty value and
    logs the failure at WARNING or ERROR level. No exception propagates to
    the caller.

    Args:
        settings:       Application settings object. Must expose
                        ``GOOGLE_SHEETS_SA_CREDENTIALS`` and ``SHEET_ID``.
        secret_manager: Optional secret manager (reserved for future use).
                        Pass ``None`` when credentials come directly from
                        ``settings``.

    Example::

        connector = SheetsConnector(settings=settings, secret_manager=None)
        if connector.is_enabled():
            rows = connector.fetch_rows("KPI_Data")
    """

    def __init__(self, settings: Any, secret_manager: Any = None) -> None:
        """
        Initialise the SheetsConnector.

        Attempts to build a ``gspread`` client from the service account
        credentials in ``settings``. If credentials are absent or any
        dependency is missing, the connector enters disabled mode silently.

        Args:
            settings:       Settings object with ``GOOGLE_SHEETS_SA_CREDENTIALS``
                            and ``SHEET_ID`` attributes.
            secret_manager: Reserved for future credential injection.
                            Pass ``None`` to use settings directly.
        """
        self._settings = settings
        self._secret_manager = secret_manager
        self._client: Any = None
        self._spreadsheet: Any = None
        self._enabled: bool = False

        self._sheet_id: str = str(getattr(settings, "SHEET_ID", "") or "").strip()
        raw_creds: str = str(
            getattr(settings, "GOOGLE_SHEETS_SA_CREDENTIALS", "") or ""
        ).strip()

        if not raw_creds or not self._sheet_id:
            logger.info(
                "SheetsConnector | credentials or SHEET_ID not configured "
                "— operating in disabled mode."
            )
            return

        self._enabled = self._initialise_client(raw_creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """
        Return the connector health status.

        Returns:
            Dict with ``"connector"`` and ``"status"`` keys::

                {"connector": "google_sheets", "status": "healthy"}

            or::

                {
                    "connector": "google_sheets",
                    "status":    "degraded",
                    "reason":    "credentials_not_configured",
                }
        """
        if self._enabled and self._client is not None:
            logger.debug("SheetsConnector.health_check | status=healthy")
            return {"connector": "google_sheets", "status": "healthy"}

        reason = (
            "credentials_not_configured"
            if not str(getattr(self._settings, "GOOGLE_SHEETS_SA_CREDENTIALS", "") or "").strip()
            else "client_unavailable"
        )
        logger.debug("SheetsConnector.health_check | status=degraded | reason=%s", reason)
        return {
            "connector": "google_sheets",
            "status":    "degraded",
            "reason":    reason,
        }

    def is_enabled(self) -> bool:
        """
        Return ``True`` if the connector is active and ready to use.

        Returns:
            ``True`` when a valid gspread client is initialised;
            ``False`` in disabled or degraded mode.
        """
        return self._enabled and self._client is not None

    def fetch_rows(self, sheet_name: str) -> list[dict[str, Any]]:
        """
        Fetch all data rows from ``sheet_name`` as a list of dicts.

        Uses the first row of the sheet as column headers. Returns an empty
        list on any failure — never raises to the caller.

        Args:
            sheet_name: Name of the worksheet tab to read from
                        (e.g. ``"KPI_Data"``).

        Returns:
            List of row dicts keyed by header names, e.g.::

                [{"date": "2025-01-01", "sales": "100"}, ...]

            Empty list when the connector is disabled, the sheet does not
            exist, or the API call fails.

        Example::

            rows = connector.fetch_rows("KPI_Data")
            for row in rows:
                print(row["date"], row["sales"])
        """
        logger.info(
            "SheetsConnector.fetch_rows | sheet=%r | enabled=%s",
            sheet_name, self._enabled,
        )

        if not self.is_enabled():
            logger.warning(
                "SheetsConnector.fetch_rows | connector disabled — returning []"
            )
            return []

        try:
            worksheet = self._get_worksheet(sheet_name)
            if worksheet is None:
                return []

            all_values: list[list[str]] = worksheet.get_all_values()
            if not all_values:
                logger.info(
                    "SheetsConnector.fetch_rows | sheet=%r is empty", sheet_name
                )
                return []

            headers = all_values[0]
            rows: list[dict[str, Any]] = []
            for raw_row in all_values[1:]:
                # Pad short rows to match header length.
                padded = raw_row + [""] * (len(headers) - len(raw_row))
                rows.append(dict(zip(headers, padded)))

            logger.info(
                "SheetsConnector.fetch_rows | sheet=%r | rows=%d",
                sheet_name, len(rows),
            )
            return rows

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SheetsConnector.fetch_rows | sheet=%r | error=%s",
                sheet_name, exc,
            )
            return []

    def append_row(self, sheet_name: str, row: list[Any]) -> bool:
        """
        Append a single row of values to ``sheet_name``.

        Args:
            sheet_name: Name of the worksheet tab to append to.
            row:        Flat list of cell values to append as one row.
                        Values are coerced to strings.

        Returns:
            ``True`` on success; ``False`` on any failure.

        Example::

            success = connector.append_row(
                "IncidentLog",
                ["INC-42", "2025-01-15", "CRITICAL", "CPU spike"],
            )
        """
        logger.info(
            "SheetsConnector.append_row | sheet=%r | cols=%d | enabled=%s",
            sheet_name, len(row), self._enabled,
        )

        if not self.is_enabled():
            logger.warning(
                "SheetsConnector.append_row | connector disabled — returning False"
            )
            return False

        try:
            worksheet = self._get_worksheet(sheet_name)
            if worksheet is None:
                return False

            str_row = [str(v) for v in row]
            worksheet.append_row(str_row)

            logger.info(
                "SheetsConnector.append_row | sheet=%r | SUCCESS", sheet_name
            )
            return True

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SheetsConnector.append_row | sheet=%r | error=%s",
                sheet_name, exc,
            )
            return False

    def close(self) -> None:
        """
        Release connector resources.

        Safe no-op — gspread clients hold no persistent connections.
        Resets internal state so ``is_enabled()`` returns ``False`` after
        closing.
        """
        logger.debug("SheetsConnector.close | releasing resources.")
        self._client = None
        self._spreadsheet = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _initialise_client(self, raw_creds: str) -> bool:
        """
        Build and validate the gspread client from service account credentials.

        Performs a lazy import of ``gspread`` and ``google-auth`` so that
        missing libraries degrade gracefully.

        Args:
            raw_creds: JSON string of the service account credentials.
                       Never logged.

        Returns:
            ``True`` if the client was successfully built and the spreadsheet
            opened; ``False`` otherwise.
        """
        try:
            import gspread  # type: ignore[import]
            from google.oauth2.service_account import Credentials  # type: ignore[import]
        except ImportError:
            logger.warning(
                "SheetsConnector | 'gspread' or 'google-auth' not installed "
                "— connector disabled. Install with: pip install gspread google-auth"
            )
            return False

        try:
            creds_dict: dict[str, Any] = self._parse_credentials(raw_creds)
            if not creds_dict:
                return False

            credentials = Credentials.from_service_account_info(
                creds_dict, scopes=_SCOPES
            )
            self._client = gspread.authorize(credentials)
            self._spreadsheet = self._client.open_by_key(self._sheet_id)

            logger.info(
                "SheetsConnector | client initialised | sheet_id=%s",
                self._sheet_id,
            )
            return True

        except Exception as exc:  # noqa: BLE001
            # Never log raw_creds or creds_dict.
            logger.error(
                "SheetsConnector | failed to initialise client: %s", exc
            )
            return False

    @staticmethod
    def _parse_credentials(raw: str) -> dict[str, Any]:
        """
        Parse the service account credential string into a dict.

        Accepts either a JSON string or a filesystem path to a JSON file.
        Credentials are never logged.

        Args:
            raw: JSON string or path string pointing to a credentials file.

        Returns:
            Parsed credentials dict, or empty dict on failure.
        """
        # Try direct JSON parse first.
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # Treat as file path.
        try:
            path = Path(raw)
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001
            pass

        logger.error(
            "SheetsConnector._parse_credentials | could not parse credentials "
            "(not valid JSON and not a readable file path)."
        )
        return {}

    def _get_worksheet(self, sheet_name: str) -> Any:
        """
        Return the gspread Worksheet object for ``sheet_name``.

        Args:
            sheet_name: Name of the worksheet tab.

        Returns:
            gspread ``Worksheet`` object, or ``None`` if not found.
        """
        if self._spreadsheet is None:
            return None

        try:
            return self._spreadsheet.worksheet(sheet_name)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SheetsConnector._get_worksheet | sheet=%r | error=%s",
                sheet_name, exc,
            )
            return None

    def __repr__(self) -> str:
        return (
            f"SheetsConnector("
            f"sheet_id={self._sheet_id!r}, "
            f"enabled={self._enabled})"
        )