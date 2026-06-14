"""
aeam/agents/action/jira_actions.py

Jira integration for the AEAM Action layer.

Creates Jira tickets for incidents via the Jira REST API v2. Called
exclusively through the ActionAgent registry — never directly by any
other component.

Phase 6 constraints:
- No retry logic (handled by ActionAgent).
- No LLM usage.
- No decision or Orchestrator logic.
- requests library only.
- HTTP timeout: 10 seconds.
- Raises on non-201 responses.
- Fully typed, logging throughout.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Enforced HTTP timeout for all Jira API calls (Phase 6 spec).
_HTTP_TIMEOUT: int = 10


class JiraActions:
    """
    Jira ticket creation integration for the AEAM Action layer.

    Retrieves credentials from the injected ``settings`` object,
    constructs the Jira REST API v2 payload, and POSTs to ``/rest/api/2/issue``.
    Raises on any non-201 response.

    This class:
    - Contains no retry logic (ActionAgent handles retries).
    - Makes no LLM calls.
    - Contains no decision or Orchestrator logic.

    Settings expected:
    - ``JIRA_URL``            — base URL, e.g. ``"https://myorg.atlassian.net"``.
    - ``JIRA_USER_EMAIL``     — Atlassian account email for Basic Auth.
    - ``JIRA_API_TOKEN``      — Jira API token.
    - ``JIRA_PROJECT_KEY``    — default project key (e.g. ``"OPS"``).
    - ``JIRA_ISSUE_TYPE``     — Jira issue type ID (e.g. ``"10004"``).

    Args:
        settings: Application settings object.

    Raises:
        ValueError: If ``settings`` is None or required attributes missing.

    Example::

        jira = JiraActions(settings=settings)
        result = jira.execute({
            "summary":     "CPU spike on web-01",
            "description": "CPU reached 97% at 14:32 UTC.",
            "priority":    "High",
        })
        # {"ticket_id": "OPS-123", "url": "https://myorg.atlassian.net/browse/OPS-123"}
    """

    def __init__(self, settings: Any) -> None:
        """
        Initialise JiraActions with an injected settings object.

        Args:
            settings: Application settings object. Must not be None.

        Raises:
            ValueError: If ``settings`` is None or required attributes missing.
        """
        if settings is None:
            raise ValueError("settings must not be None.")
        
        self.settings = settings  # store for later use

        # Normalise JIRA_URL: remove trailing slash if present.
        jira_url_raw = getattr(settings, "JIRA_URL", None)
        if not jira_url_raw:
            raise ValueError("settings.JIRA_URL must be non-empty.")
        self.base_url = jira_url_raw.rstrip("/")
        
        self.email = getattr(settings, "JIRA_USER_EMAIL", None)
        self.api_token = getattr(settings, "JIRA_API_TOKEN", None)
        if not self.email or not self.api_token:
            raise ValueError("settings.JIRA_USER_EMAIL and JIRA_API_TOKEN must be non-empty.")
        self.auth = (self.email, self.api_token)
        
        self.project_key = getattr(settings, "JIRA_PROJECT_KEY", None)
        if not self.project_key:
            raise ValueError("settings.JIRA_PROJECT_KEY must be non-empty.")
        
        self.issue_type_id = getattr(settings, "JIRA_ISSUE_TYPE", None)
        if not self.issue_type_id:
            raise ValueError("settings.JIRA_ISSUE_TYPE must be non-empty (issue type ID).")

    # ------------------------------------------------------------------
    # ActionAgent registry interface
    # ------------------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ActionAgent registry entry point — delegates to :meth:`create_ticket`.

        Args:
            params: Action parameters dict. See :meth:`create_ticket`.

        Returns:
            Result dict from :meth:`create_ticket`.
        """
        return self.create_ticket(params)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_ticket(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Create a Jira issue via the REST API v2.

        Steps:
        1. Use the stored Jira configuration from settings.
        2. Validate required parameters (``summary``).
        3. Build the Jira issue payload using issue type ID.
        4. POST to ``/rest/api/2/issue`` with Basic Auth.
        5. Raise :class:`requests.HTTPError` if the response status is not 201.
        6. Parse and return the ticket ID and browse URL.

        Args:
            params: Dict containing:

                - ``"summary"``     *(required)* — issue title.
                - ``"description"`` *(optional)* — issue body. Defaults to ``""``.
                - ``"priority"``    *(optional)* — Jira priority name
                  (e.g. ``"High"``). Defaults to ``"Medium"``.
                - ``"project_key"`` *(optional)* — override the default project
                  key from settings.
                - ``"labels"``      *(optional)* — list of label strings.

        Returns:
            Dict::

                {
                    "ticket_id": str,  # e.g. "OPS-123"
                    "url":       str,  # e.g. "https://myorg.atlassian.net/browse/OPS-123"
                }

        Raises:
            ValueError:            If ``summary`` is missing or blank.
            requests.HTTPError:    If the Jira API returns a non-201 status.
            requests.Timeout:      If the request exceeds 10 seconds.
            requests.ConnectionError: If the Jira host is unreachable.

        Example::

            result = jira.create_ticket({
                "summary":     "Disk I/O degradation on db-02",
                "description": "Disk I/O exceeded threshold for 10 consecutive minutes.",
                "priority":    "High",
                "labels":      ["aeam", "auto-generated"],
            })
        """
        # Step 1: configuration already stored in self.
        # Step 2: validate required parameters.
        summary: str = params.get("summary", "").strip()
        if not summary:
            raise ValueError("params['summary'] must be a non-empty string.")

        description: str = params.get("description", "")
        priority: str = params.get("priority", "Medium")
        project_key: str = params.get("project_key", self.project_key)
        labels: list[str] = params.get("labels", [])

        # Step 3: build payload — use issue type ID from settings.
        payload: dict[str, Any] = {
            "fields": {
                "project":   {"key": project_key},
                "summary":   summary,
                "description": description,
                "issuetype": {"id": self.issue_type_id},   # <-- changed from name to id
                "priority":  {"name": priority},
            }
        }
        if labels:
            payload["fields"]["labels"] = labels

        endpoint = f"{self.base_url}/rest/api/2/issue"

        logger.info(
            "JiraActions.create_ticket | POST %s | project=%s | priority=%s | "
            "summary=%r | issuetype_id=%s",
            endpoint, project_key, priority, summary, self.issue_type_id,
        )

        # Step 4: POST to Jira.
        response = requests.post(
            url=endpoint,
            json=payload,
            auth=self.auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )

        # Step 5: raise on non-201.
        if response.status_code != 201:
            logger.error(
                "JiraActions.create_ticket | FAILED | status=%d | body=%s",
                response.status_code, response.text[:500],
            )
            response.raise_for_status()

        # Step 6: parse response.
        body: dict[str, Any] = response.json()
        ticket_id: str = body["key"]
        ticket_url: str = f"{self.base_url}/browse/{ticket_id}"

        logger.info(
            "JiraActions.create_ticket | SUCCESS | ticket_id=%s | url=%s",
            ticket_id, ticket_url,
        )

        return {
            "ticket_id": ticket_id,
            "url":       ticket_url,
        }

    def __repr__(self) -> str:
        return "JiraActions()"