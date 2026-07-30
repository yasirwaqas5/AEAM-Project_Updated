"""Enterprise connector implementations (Phase F7).

Eight connectors, one contract. Four yield documents (SharePoint, Confluence,
GitHub, Google Workspace) and four yield metrics (SAP, Salesforce, Snowflake,
BigQuery); every one of them is an
:class:`~aeam.connectors.base.EnterpriseConnector` and reaches the platform
through the existing ingestion pipeline or the existing
``CompositeKPISource`` — never a path of its own.

Each connector takes its upstream client by injection. That single seam is
what keeps gating CI offline: a test (or an operator running in mock mode)
passes a deterministic in-repo client from :mod:`aeam.connectors.enterprise.mock`,
so the shared contract suite exercises real connector logic against a fake
transport and never makes a third-party call.
"""
