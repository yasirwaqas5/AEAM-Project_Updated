"""
aeam/agents/action/sheets_actions.py

Google Sheets integration for the AEAM Action layer.

Appends incident records to a Google Sheets spreadsheet via the Sheets
REST API v4. Authenticates using a service account JWT assertion (same
OAuth2 flow as EmailActions). Called exclusively through the ActionAgent
registry.

Phase 6 constraints:
- No retry logic (handled by ActionAgent).
- No LLM usage.
- No decision or Orchestrator logic.
- requests library only.
- HTTP timeout: 10 seconds.
- Raises on non-200 responses.
- Fully typed, logging throughout.
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
from typing import Any

import requests

logger = get_logger(__name__, agent="action")

# Enforced HTTP timeout (Phase 6 spec).
_HTTP_TIMEOUT: int = 10

# Google Sheets API v4 base URL.
_SHEETS_BASE_URL: str = "https://sheets.googleapis.com/v4/spreadsheets"

# Google OAuth2 token endpoint.
_GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"

# Required OAuth2 scope for reading and writing Sheets data.
_SHEETS_SCOPE: str = "https://www.googleapis.com/auth/spreadsheets"


class GoogleSheetsActions:
    """
    Google Sheets row-append integration for the AEAM Action layer.

    Authenticates with the Sheets API using a service account OAuth2 token,
    then calls the ``spreadsheets.values.append`` endpoint to insert a new
    row into the specified sheet.

    This class:
    - Contains no retry logic (ActionAgent handles retries).
    - Makes no LLM calls.
    - Contains no decision or Orchestrator logic.

    Secrets expected from ``secret_manager``:
    - ``"sheets_client_email"`` — service account email address.
    - ``"sheets_private_key"``  — RSA private key (PEM string) for JWT signing.

    Args:
        secret_manager: Secrets provider with a ``get(key: str) -> str`` interface.

    Raises:
        ValueError: If ``secret_manager`` is None.

    Example::

        sheets = GoogleSheetsActions(secret_manager=secret_manager)
        result = sheets.execute({
            "spreadsheet_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
            "sheet_name":     "Incidents",
            "values":         ["INC-42", "2024-01-15T14:32:00Z", "CRITICAL", "CPU spike"],
        })
        # {"updated_range": "Incidents!A5:D5", "updated_rows": 1}
    """

    def __init__(self, secret_manager: Any) -> None:
        """
        Initialise GoogleSheetsActions with an injected secrets provider.

        Args:
            secret_manager: Secrets provider. Must not be None.

        Raises:
            ValueError: If ``secret_manager`` is None.
        """
        if secret_manager is None:
            raise ValueError("secret_manager must not be None.")
        self._secrets: Any = secret_manager

    # ------------------------------------------------------------------
    # ActionAgent registry interface
    # ------------------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ActionAgent registry entry point — delegates to :meth:`append_row`.

        Args:
            params: Action parameters dict. See :meth:`append_row`.

        Returns:
            Result dict from :meth:`append_row`.
        """
        return self.append_row(params)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_row(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Append a row of values to a Google Sheets spreadsheet.

        Steps:
        1. Retrieve ``sheets_client_email`` and ``sheets_private_key`` from
           ``SecretManager`` and obtain a short-lived OAuth2 bearer token.
        2. Validate required parameters (``spreadsheet_id``, ``sheet_name``,
           ``values``).
        3. Build the Sheets API v4 ``values.append`` URL and request body.
        4. POST to the Sheets API with ``valueInputOption=USER_ENTERED`` and
           ``insertDataOption=INSERT_ROWS``.
        5. Raise :class:`requests.HTTPError` if the response status is not 200.
        6. Return the updated range and row count from the API response.

        Args:
            params: Dict containing:

                - ``"spreadsheet_id"`` *(required)* — Google Sheets spreadsheet
                  ID (the long alphanumeric string in the sheet URL).
                - ``"sheet_name"``     *(required)* — Name of the target tab/
                  sheet within the spreadsheet (e.g. ``"Incidents"``).
                - ``"values"``         *(required)* — Flat list of cell values
                  to append as a single row. Values are coerced to strings.
                  Example: ``["INC-42", "2024-01-15", "CRITICAL", "CPU spike"]``
                - ``"value_input_option"`` *(optional)* — How input data should
                  be interpreted. ``"USER_ENTERED"`` (default) or ``"RAW"``.

        Returns:
            Dict::

                {
                    "updated_range": str,  # e.g. "Incidents!A5:D5"
                    "updated_rows":  int,  # number of rows appended (always 1)
                }

        Raises:
            ValueError:               If ``spreadsheet_id``, ``sheet_name``
                                      are empty, or ``values`` is not a
                                      non-empty list.
            requests.HTTPError:       If the Sheets API returns a non-200
                                      status.
            requests.Timeout:         If any request exceeds 10 seconds.
            requests.ConnectionError: If the Sheets API is unreachable.
            RuntimeError:             If the OAuth2 token exchange fails.

        Example::

            result = sheets.append_row({
                "spreadsheet_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
                "sheet_name":     "Incidents",
                "values":         ["INC-42", "2024-01-15T14:32:00Z", "CRITICAL",
                                   "CPU spike on web-01", "ops@example.com"],
            })
        """
        # Step 1: retrieve secrets and obtain access token.
        client_email: str = self._secrets.get("sheets_client_email")
        private_key: str = self._secrets.get("sheets_private_key")
        access_token: str = self._get_access_token(
            client_email=client_email,
            private_key=private_key,
        )

        # Step 2: validate parameters.
        spreadsheet_id: str = params.get("spreadsheet_id", "").strip()
        if not spreadsheet_id:
            raise ValueError("params['spreadsheet_id'] must be a non-empty string.")

        sheet_name: str = params.get("sheet_name", "").strip()
        if not sheet_name:
            raise ValueError("params['sheet_name'] must be a non-empty string.")

        values: list[Any] = params.get("values", [])
        if not isinstance(values, list) or len(values) == 0:
            raise ValueError("params['values'] must be a non-empty list.")

        value_input_option: str = params.get("value_input_option", "USER_ENTERED")

        # Step 3: build URL and payload.
        # Range uses the sheet name as the scope; Sheets API appends after
        # the last occupied row automatically.
        range_notation: str = f"{sheet_name}!A1"
        url: str = (
            f"{_SHEETS_BASE_URL}/{spreadsheet_id}"
            f"/values/{requests.utils.quote(range_notation, safe='')}:append"
        )

        # Sheets API expects a 2D array (list of rows); we always append one row.
        body: dict[str, Any] = {
            "values": [[str(v) for v in values]],
        }

        query_params: dict[str, str] = {
            "valueInputOption": value_input_option,
            "insertDataOption": "INSERT_ROWS",
        }

        logger.info(
            "GoogleSheetsActions.append_row | spreadsheet=%s | sheet=%s | "
            "columns=%d",
            spreadsheet_id, sheet_name, len(values),
        )

        # Step 4: POST to Sheets API.
        response = requests.post(
            url=url,
            json=body,
            params=query_params,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            timeout=_HTTP_TIMEOUT,
        )

        # Step 5: raise on non-200.
        if response.status_code != 200:
            logger.error(
                "GoogleSheetsActions.append_row | FAILED | status=%d | body=%s",
                response.status_code, response.text[:500],
            )
            response.raise_for_status()

        # Step 6: parse and return result.
        resp_body: dict[str, Any] = response.json()
        updates: dict[str, Any] = resp_body.get("updates", {})

        updated_range: str = updates.get("updatedRange", "")
        updated_rows: int = updates.get("updatedRows", 1)

        logger.info(
            "GoogleSheetsActions.append_row | SUCCESS | updated_range=%s | "
            "updated_rows=%d",
            updated_range, updated_rows,
        )

        return {
            "updated_range": updated_range,
            "updated_rows":  updated_rows,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_access_token(
        self,
        client_email: str,
        private_key: str,
    ) -> str:
        """
        Exchange a JWT service account assertion for a Google OAuth2 bearer token.

        Constructs and signs a JWT using ``PyJWT``, then POSTs to the Google
        token endpoint to obtain a short-lived access token scoped to
        ``spreadsheets`` (read/write).

        Args:
            client_email: Service account email address (``iss`` claim).
            private_key:  PEM-encoded RSA private key for RS256 signing.

        Returns:
            Short-lived OAuth2 access token string.

        Raises:
            RuntimeError:       If the token response is missing ``access_token``.
            requests.HTTPError: If the token exchange request fails (non-200).
            requests.Timeout:   If the token exchange exceeds 10 seconds.
            ImportError:        If ``PyJWT`` is not installed.
        """
        import time
        import jwt  # pip install PyJWT[cryptography]

        now = int(time.time())
        claims = {
            "iss":   client_email,
            "sub":   client_email,
            "scope": _SHEETS_SCOPE,
            "aud":   _GOOGLE_TOKEN_URL,
            "iat":   now,
            "exp":   now + 3600,
        }

        assertion: str = jwt.encode(claims, private_key, algorithm="RS256")

        logger.debug(
            "GoogleSheetsActions._get_access_token | requesting token for %s",
            client_email,
        )

        token_response = requests.post(
            url=_GOOGLE_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_HTTP_TIMEOUT,
        )

        if token_response.status_code != 200:
            logger.error(
                "_get_access_token | token exchange failed | status=%d | body=%s",
                token_response.status_code, token_response.text[:500],
            )
            token_response.raise_for_status()

        token_data: dict[str, Any] = token_response.json()
        access_token: str | None = token_data.get("access_token")

        if not access_token:
            raise RuntimeError(
                f"OAuth2 token exchange succeeded but response contained no "
                f"'access_token'. Response keys: {list(token_data.keys())}."
            )

        logger.debug(
            "GoogleSheetsActions._get_access_token | token obtained successfully."
        )
        return access_token

    def __repr__(self) -> str:
        return "GoogleSheetsActions()"