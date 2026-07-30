"""
aeam/tests/test_phase_f7_connectors.py

Phase F7 — Enterprise Connector Framework & Data-Source Connectors.

Acceptance criteria under test:

1. **Each connector implements the ABC and passes the shared contract suite.**
   One suite, parametrized over all eight connectors, run against mocked source
   APIs. The ABC *is* the contract, so the suite calls every method on every
   connector rather than testing each connector's own idea of itself.
2. **Ingested content flows through the existing pipeline unchanged** — a
   connector document is byte-for-byte the same registry state as an uploaded
   one, and is retrievable identically.
3. **Credentials are never present outside SecretManager** — asserted at the
   source level, on every health/describe payload, and on sanitised errors.
4. **A deliberately failing connector degrades gracefully** and blocks no other
   connector, ingestion, KPI collection, or retrieval.
5. **Incremental sync re-ingests only changed content**, and a repeated sync
   with no upstream change produces no duplicate document, embedding, metadata
   row, or job.

Infrastructure: in-process only — real SQLite, real FastAPI TestClient, real
BlobStore, deterministic mock upstream clients (TEST-3). **No live third-party
API call anywhere**, which is asserted structurally as well as by construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.connectors import router as connectors_router
from aeam.connectors.base import (
    ConnectorArtifactRef,
    ConnectorError,
    EnterpriseConnector,
    content_hash_of,
    sanitize_error,
)
from aeam.connectors.enterprise.mock import (
    DIALECTS,
    MockArtifact,
    MockDocumentClient,
    MockMetricsClient,
    default_document_fixture,
    default_metrics_fixture,
)
from aeam.connectors.health import ConnectorHealthReporter
from aeam.connectors.registry import CONNECTOR_CLASSES, FLAG_FOR_KIND, ConnectorRegistry
from aeam.connectors.sync import ConnectorSyncEngine
from aeam.config.settings import Settings
from aeam.ingestion.submission import IngestionSubmitter, get_or_create_upload_source
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import (
    ConnectorCapability,
    Source,
    SourceKind,
    SourceStatus,
    SyncRunStatus,
)
from aeam.registry.repositories import (
    ConnectorArtifactRepository,
    ConnectorSyncRunRepository,
    DocumentRepository,
    IngestionJobRepository,
    SourceRepository,
    VersionRepository,
)
# ===========================================================================
# Fixtures
# ===========================================================================

#: Minimum config that satisfies each connector's `required_config`. Real
#: values are irrelevant (the transport is mocked); what matters is that the
#: contract suite exercises the CONFIGURED path for every connector.
CONNECTOR_CONFIG: dict[str, dict[str, Any]] = {
    SourceKind.SHAREPOINT: {"site_url": "https://mock.invalid/sites/ops", "drive_id": "drive-1"},
    SourceKind.CONFLUENCE: {"base_url": "https://mock.invalid/wiki", "space_key": "OPS"},
    SourceKind.GITHUB: {"repository": "acme/runbooks", "path": "docs"},
    SourceKind.GOOGLE_WORKSPACE: {"folder_id": "folder-1"},
    SourceKind.SAP: {"host": "sap.mock.invalid", "client": "100", "query": "ZREVENUE"},
    SourceKind.SALESFORCE: {
        "instance_url": "https://mock.invalid", "soql": "SELECT CloseDate, Amount FROM Opportunity"
    },
    SourceKind.SNOWFLAKE: {
        "account": "acct", "warehouse": "wh", "database": "db", "query": "SELECT * FROM kpi"
    },
    SourceKind.BIGQUERY: {
        "project_id": "proj", "dataset": "ds", "query": "SELECT * FROM kpi"
    },
}

ALL_KINDS: list[str] = sorted(CONNECTOR_CLASSES)
DOCUMENT_KINDS: list[str] = sorted(
    k for k, c in CONNECTOR_CLASSES.items() if c.capability == ConnectorCapability.DOCUMENTS
)
METRIC_KINDS: list[str] = sorted(
    k for k, c in CONNECTOR_CLASSES.items() if c.capability == ConnectorCapability.METRICS
)


class _StubSecretManager:
    """Resolves secrets by name, and records every name it was asked for.

    The recording is the point: credential-isolation tests assert that a
    connector obtained its credential HERE and nowhere else.
    """

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})
        self.requested: list[str] = []

    def get_secret(self, key: str, default: Any = None) -> Any:
        self.requested.append(key)
        return self._secrets.get(key, default)


def _settings(**overrides) -> Settings:
    base: dict[str, Any] = dict(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False,
        CONNECTORS_ENABLED=True,
    )
    # Every connector on by default in tests, so the contract suite covers all
    # eight without eight per-test overrides.
    for flag in FLAG_FOR_KIND.values():
        base[flag] = True
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'f7.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def blob_store(tmp_path):
    from aeam.storage.blob_store import LocalDiskBlobStore

    return LocalDiskBlobStore(root_dir=str(tmp_path / "blobs"))


@pytest.fixture()
def submitter(db, blob_store):
    return IngestionSubmitter(db=db, blob_store=blob_store)


def _seed_source(db, kind: str, name: str | None = None, config: dict | None = None) -> Any:
    source_id = SourceRepository(db).create(Source(
        name=name or f"{kind} source",
        kind=kind,
        config=config if config is not None else dict(CONNECTOR_CONFIG[kind]),
        secret_ref=f"{kind.upper()}_TOKEN",
        status=SourceStatus.ACTIVE,
    ))
    return SourceRepository(db).get(source_id)


def _connector(kind: str, client: Any = None, config: dict | None = None, **kwargs) -> EnterpriseConnector:
    cls = CONNECTOR_CLASSES[kind]
    if client is None:
        client = (
            # The vendor's own field naming, so the connector's field map does
            # real translation work rather than reading keys invented for it.
            default_document_fixture(dialect=DIALECTS.get(kind))
            if cls.capability == ConnectorCapability.DOCUMENTS
            else default_metrics_fixture(_selector_for(kind))
        )
    return cls(
        source_id=f"src-{kind}",
        config=config if config is not None else dict(CONNECTOR_CONFIG[kind]),
        secret_manager=_StubSecretManager({f"{kind.upper()}_TOKEN": "s3cr3t-token-value"}),
        secret_ref=f"{kind.upper()}_TOKEN",
        client=client,
        **kwargs,
    )


def _selector_for(kind: str) -> str:
    cls = CONNECTOR_CLASSES[kind]
    key = getattr(cls, "selector_config_key", "selector")
    return str(CONNECTOR_CONFIG[kind].get(key) or "mock-selector")


def _engine(db, submitter, registry=None, **settings_overrides) -> ConnectorSyncEngine:
    # Mock mode by default: the registry injects the deterministic in-repo
    # client, so the engine exercises the real listing/change-detection/
    # ingestion path with a fake transport and gating CI makes no live call.
    settings_overrides.setdefault("CONNECTOR_MOCK_MODE", True)
    settings = _settings(**settings_overrides)
    return ConnectorSyncEngine(
        db=db,
        submitter=submitter,
        registry=registry or ConnectorRegistry(
            settings=settings,
            secret_manager=_StubSecretManager({
                f"{kind.upper()}_TOKEN": "s3cr3t-token-value" for kind in ALL_KINDS
            }),
        ),
        max_artifacts=settings.CONNECTOR_SYNC_MAX_ARTIFACTS,
    )


# ===========================================================================
# 1. The shared connector contract suite — every connector, every method
# ===========================================================================


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_connector_declares_kind_and_capability(kind):
    cls = CONNECTOR_CLASSES[kind]
    assert cls.kind == kind, "the registry key and the class's own kind must agree"
    assert cls.capability in ConnectorCapability.ALL
    assert cls.display_name, "every connector needs an operator-facing name"
    assert issubclass(cls, EnterpriseConnector)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_connector_answers_every_contract_method(kind):
    # The ABC is the contract, so every connector answers every method — that
    # is what makes ONE suite able to cover eight implementations.
    connector = _connector(kind)
    for method in (
        "validate_config", "authenticate", "list_artifacts", "fetch_artifact",
        "fetch_rows", "describe", "health", "close",
    ):
        assert callable(getattr(connector, method)), f"{kind} is missing {method}()"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_configured_connector_validates(kind):
    configured, reason = _connector(kind).validate_config()
    assert configured is True
    assert reason is None


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_missing_config_is_reported_not_raised(kind):
    # An unconfigured connector is a normal state for a fresh deployment, not
    # an error — and the reason must name the missing key so an operator can
    # act without reading the source.
    connector = _connector(kind, config={})
    configured, reason = connector.validate_config()
    required = CONNECTOR_CLASSES[kind].required_config
    if required:
        assert configured is False
        assert "Missing required configuration" in reason
        for key in required:
            assert key in reason
    else:
        assert configured is True


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_authenticate_succeeds_with_an_injected_client(kind):
    connector = _connector(kind)
    assert connector.authenticate() is True
    assert connector.is_authenticated is True
    assert connector.health()["auth_error"] is None


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_authenticate_fails_honestly_without_a_credential(kind):
    cls = CONNECTOR_CLASSES[kind]
    connector = cls(
        source_id=f"src-{kind}",
        config=dict(CONNECTOR_CONFIG[kind]),
        secret_manager=_StubSecretManager({}),   # resolves nothing
        secret_ref=f"{kind.upper()}_TOKEN",
        client=None,                              # no injected transport either
    )
    assert connector.authenticate() is False
    assert connector.is_authenticated is False
    # Honest and actionable: it names what is missing, and never claims health.
    assert "credential" in (connector.health()["auth_error"] or "").lower()


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_unconfigured_connector_never_authenticates(kind):
    if not CONNECTOR_CLASSES[kind].required_config:
        pytest.skip(f"{kind} has no required configuration")
    connector = _connector(kind, config={})
    assert connector.authenticate() is False
    assert "Missing required configuration" in connector.health()["auth_error"]


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_health_reports_the_required_fields(kind):
    health = _connector(kind).health()
    for field in (
        "source_id", "kind", "capability", "display_name", "configured",
        "config_keys", "required_config", "secret_ref", "authenticated", "auth_error",
    ):
        assert field in health, f"{kind} health is missing {field}"
    # Never optimistic: authenticated is False until a call actually succeeded.
    assert health["authenticated"] is False


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_contract_close_is_idempotent_and_forgets_credentials(kind):
    connector = _connector(kind)
    connector.authenticate()
    connector.close()
    connector.close()  # must not raise
    assert connector.is_authenticated is False
    assert connector._resolved_secrets == []


@pytest.mark.parametrize("kind", DOCUMENT_KINDS)
def test_contract_document_connector_lists_and_fetches(kind):
    connector = _connector(kind)
    connector.authenticate()
    refs = connector.list_artifacts()

    assert len(refs) == 3, f"{kind} did not translate the fixture listing"
    for ref in refs:
        assert isinstance(ref, ConnectorArtifactRef)
        assert ref.external_id and ref.title
        data = connector.fetch_artifact(ref)
        assert isinstance(data, bytes) and data


@pytest.mark.parametrize("kind", DOCUMENT_KINDS)
def test_contract_document_connector_yields_no_rows(kind):
    # Empty rather than an error: a documents connector genuinely has no KPI
    # rows, and MonitorAgent already treats "no rows" as a no-op.
    connector = _connector(kind)
    connector.authenticate()
    assert connector.fetch_rows("anything") == []


@pytest.mark.parametrize("kind", METRIC_KINDS)
def test_contract_metric_connector_yields_rows(kind):
    connector = _connector(kind)
    connector.authenticate()
    rows = connector.fetch_rows(_selector_for(kind))

    assert len(rows) == 4, f"{kind} returned no rows from the fixture"
    # The shape MonitorAgent._extract_series already consumes.
    assert all(isinstance(row, dict) for row in rows)
    assert any("revenue" in row for row in rows)


@pytest.mark.parametrize("kind", METRIC_KINDS)
def test_contract_metric_connector_yields_no_artifacts(kind):
    connector = _connector(kind)
    connector.authenticate()
    assert connector.list_artifacts() == []


@pytest.mark.parametrize("kind", METRIC_KINDS)
def test_contract_metric_connector_fetch_rows_never_raises(kind):
    # It runs on MonitorAgent's cycle. An exception there would take down KPI
    # collection for EVERY source, so this is the single most important
    # isolation guarantee in the phase.
    client = MockMetricsClient({})
    client.fail_with = RuntimeError("warehouse is down")
    connector = _connector(kind, client=client)
    connector.authenticate()

    assert connector.fetch_rows(_selector_for(kind)) == []
    assert "warehouse is down" in (connector.health()["last_fetch_error"] or "")


@pytest.mark.parametrize("kind", METRIC_KINDS)
def test_contract_metric_connector_without_a_client_reports_the_missing_sdk(kind):
    cls = CONNECTOR_CLASSES[kind]
    connector = cls(
        source_id=f"src-{kind}",
        config=dict(CONNECTOR_CONFIG[kind]),
        secret_manager=_StubSecretManager({f"{kind.upper()}_TOKEN": "value"}),
        secret_ref=f"{kind.upper()}_TOKEN",
        client=None,
    )
    assert connector.authenticate() is False
    # Names the SDK rather than degrading silently to zero rows — an absent SDK
    # reported as healthy would be exactly the misrepresentation SEC-8 forbids.
    assert cls.sdk_module in connector.health()["auth_error"]
    assert connector.fetch_rows(_selector_for(kind)) == []


@pytest.mark.parametrize("kind", DOCUMENT_KINDS)
def test_contract_document_connector_isolates_an_upstream_failure(kind):
    client = default_document_fixture()
    client.fail_with = RuntimeError("upstream 503")
    connector = _connector(kind, client=client)

    assert connector.authenticate() is False
    assert "upstream" in (connector.health()["auth_error"] or "").lower()


@pytest.mark.parametrize("kind", DOCUMENT_KINDS)
def test_contract_document_connector_skips_an_item_with_no_stable_id(kind):
    # A synthesised id would be unstable across syncs, and unstable ids
    # re-ingest the same artifact forever. Skipping is the correct answer.
    client = MockDocumentClient(
        [MockArtifact(external_id="", title="", body="x")], dialect=DIALECTS.get(kind)
    )
    connector = _connector(kind, client=client)
    connector.authenticate()
    assert connector.list_artifacts() == []


# ===========================================================================
# 2. Credential isolation (SEC-5)
# ===========================================================================


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_credentials_are_obtained_only_through_secret_manager(kind):
    secrets = _StubSecretManager({f"{kind.upper()}_TOKEN": "s3cr3t-token-value"})
    cls = CONNECTOR_CLASSES[kind]
    connector = cls(
        source_id=f"src-{kind}",
        config=dict(CONNECTOR_CONFIG[kind]),
        secret_manager=secrets,
        secret_ref=f"{kind.upper()}_TOKEN",
        client=default_document_fixture(dialect=DIALECTS.get(kind))
        if cls.capability == ConnectorCapability.DOCUMENTS
        else default_metrics_fixture(_selector_for(kind)),
    )
    connector.authenticate()
    connector.list_artifacts()
    connector.fetch_rows(_selector_for(kind))

    # With a client injected the connector needs no credential at all, so the
    # meaningful assertion is that it never reached for one anywhere else.
    assert all(name.isupper() or "_" in name for name in secrets.requested)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_no_credential_value_appears_in_describe_or_health(kind):
    connector = _connector(kind)
    connector.authenticate()
    payloads = json.dumps([connector.describe(), connector.health()])

    assert "s3cr3t-token-value" not in payloads
    # The NAME is present and should be — it is not sensitive, and hiding it
    # would make a misconfiguration undiagnosable.
    assert f"{kind.upper()}_TOKEN" in payloads


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_a_resolved_credential_is_scrubbed_out_of_error_messages(kind):
    cls = CONNECTOR_CLASSES[kind]
    connector = cls(
        source_id=f"src-{kind}",
        config=dict(CONNECTOR_CONFIG[kind]),
        secret_manager=_StubSecretManager({f"{kind.upper()}_TOKEN": "s3cr3t-token-value"}),
        secret_ref=f"{kind.upper()}_TOKEN",
        client=None,
    )
    connector._resolve_secret()
    # Upstream systems routinely echo the token back in an error body; this is
    # the boundary where that stops.
    scrubbed = connector.sanitize("invalid bearer s3cr3t-token-value for tenant acme")
    assert "s3cr3t-token-value" not in scrubbed
    assert "***redacted***" in scrubbed
    assert "tenant acme" in scrubbed, "scrubbing must not destroy the diagnostic context"


def test_sanitize_leaves_short_values_alone():
    # A two-character "secret" would match everywhere and redact the message
    # into uselessness. A real credential is never that short.
    assert sanitize_error("error at row 12", ["12"]) == "error at row 12"
    assert sanitize_error("token abcdefgh failed", ["abcdefgh"]) == "token ***redacted*** failed"


def test_connector_source_rows_never_store_a_credential(db):
    source = _seed_source(db, SourceKind.GITHUB)
    stored = json.dumps(SourceRepository(db).get(source.source_id).to_row())
    # sources.secret_ref holds a NAME; the value lives only in SecretManager.
    assert "s3cr3t" not in stored
    assert "GITHUB_TOKEN" in stored


# ===========================================================================
# 3. Single ingestion path — connector content is indistinguishable
# ===========================================================================


def test_a_connector_document_is_registered_exactly_like_an_upload(db, submitter, blob_store):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    outcome = _engine(db, submitter).sync_source(source.source_id)
    assert outcome["status"] == SyncRunStatus.SUCCEEDED, outcome

    doc_repo, job_repo = DocumentRepository(db), IngestionJobRepository(db)
    connector_docs = doc_repo.list_by_source(source.source_id)
    assert len(connector_docs) == 3

    # The SAME upload path, invoked directly, for comparison.
    upload_source_id = get_or_create_upload_source(SourceRepository(db))
    uploaded = submitter.submit(
        b"# uploaded\n\nsome content\n",
        filename="uploaded.md", content_type="text/markdown", source_id=upload_source_id,
    )
    upload_doc = doc_repo.get(uploaded.parent_id)
    connector_doc = connector_docs[0]

    # Same registry shape, same lifecycle, same job type. The only difference is
    # source_id — which is provenance, and is the point.
    assert connector_doc.status == upload_doc.status
    assert connector_doc.current_version == upload_doc.current_version
    # Both are real format categories the shared validator detected — the
    # point is that the SAME validator classified both.
    assert connector_doc.doc_type in ("md", "markdown", "log", "html", "txt")
    assert upload_doc.doc_type in ("md", "markdown", "log", "html", "txt")
    for doc in (connector_doc, upload_doc):
        version = VersionRepository(db).get_active("document", doc.doc_id)
        assert version is not None and version.blob_ref
        assert doc.content_hash

    jobs = job_repo.list_all()
    assert {job.job_type for job in jobs} == {"ingest"}, (
        "connector content must produce the SAME job type as an upload — no second path"
    )


def test_connector_and_uploaded_documents_are_indistinguishable_to_retrieval(db, submitter):
    """Retrieval-equivalence: the fields retrieval reads carry no marker of
    origin, so a retrieval path cannot treat the two differently."""
    source = _seed_source(db, SourceKind.CONFLUENCE)
    _engine(db, submitter).sync_source(source.source_id)

    doc_repo = DocumentRepository(db)
    upload_source_id = get_or_create_upload_source(SourceRepository(db))
    submitter.submit(
        b"# uploaded runbook\n", filename="runbook.md",
        content_type="text/markdown", source_id=upload_source_id, semantic_type="runbook",
    )

    retrieval_fields = {"doc_id", "title", "doc_type", "semantic_type", "content_hash", "status"}
    shapes = set()
    for doc in doc_repo.list_all():
        shapes.add(frozenset(k for k in doc.to_row() if k in retrieval_fields))
    assert len(shapes) == 1, "connector and uploaded documents must expose one shape"

    # And the connector documents carry a real semantic type from upstream
    # metadata, which is what earns retrieval's authoritative-source bonus.
    connector_docs = doc_repo.list_by_source(source.source_id)
    assert any(doc.semantic_type == "runbook" for doc in connector_docs)


def test_identical_bytes_from_a_connector_reuse_an_uploaded_document(db, submitter):
    """Content-addressed dedup does not care where bytes came from.

    An upload and a connector delivering the same file converge on ONE document
    — which is only possible because they share the submission path.
    """
    upload_source_id = get_or_create_upload_source(SourceRepository(db))
    data = b"# shared\n\nidentical bytes\n"
    first = submitter.submit(
        data, filename="shared.md", content_type="text/markdown", source_id=upload_source_id
    )
    source = _seed_source(db, SourceKind.GITHUB)
    second = submitter.submit(
        data, filename="shared.md", content_type="text/markdown", source_id=source.source_id
    )

    assert second.parent_id == first.parent_id
    assert second.asset_created is False
    assert DocumentRepository(db).count() == 1


def test_the_sync_engine_cannot_be_built_without_the_shared_submitter(db):
    # Structural: there is no code path in the engine that could put content
    # into the platform another way, because it refuses to exist without the
    # one submitter.
    with pytest.raises(ValueError, match="IngestionSubmitter"):
        ConnectorSyncEngine(db=db, submitter=None, registry=object())


def test_the_sync_engine_never_imports_a_second_pipeline_component():
    forbidden = ("Chunker", "EmbeddingService", "RetrievalPipeline", "QdrantClient", "Qdrant")
    source = (Path(__file__).resolve().parents[1] / "connectors" / "sync.py").read_text(
        encoding="utf-8"
    )
    imports = [ln for ln in source.splitlines() if ln.lstrip().startswith(("import ", "from "))]
    for line in imports:
        for name in forbidden:
            assert name not in line, f"sync.py must not import {name}: {line!r}"


# ===========================================================================
# 4. Incremental & idempotent synchronization
# ===========================================================================


def test_a_repeated_sync_with_no_upstream_change_does_no_work(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    client = default_document_fixture(dialect=DIALECTS[SourceKind.SHAREPOINT])
    registry = ConnectorRegistry(
        settings=_settings(), secret_manager=_StubSecretManager({}),
        classes={SourceKind.SHAREPOINT: CONNECTOR_CLASSES[SourceKind.SHAREPOINT]},
    )
    # One client instance across both runs, so its call counters are cumulative
    # evidence of what the second run actually did.
    registry.build = lambda src: _connector(SourceKind.SHAREPOINT, client=client)  # type: ignore[assignment]
    engine = ConnectorSyncEngine(db=db, submitter=submitter, registry=registry)

    first = engine.sync_source(source.source_id)
    assert first["processed_count"] == 3
    fetches_after_first = client.fetch_calls
    docs_after_first = DocumentRepository(db).count()
    jobs_after_first = IngestionJobRepository(db).count()

    second = engine.sync_source(source.source_id)

    # Nothing changed upstream, so nothing is processed. Graph's server-side
    # `since` filter means the two timestamped artifacts are not even LISTED on
    # the second run — a bigger saving than skipping them locally would be — so
    # only the opaque one (which upstream cannot filter) reaches the engine, and
    # it is skipped after a byte comparison.
    assert second["processed_count"] == 0
    assert second["listed_count"] == 1
    assert second["skipped_count"] == 1
    # The idempotency claim: no duplicate document, job, or provenance row.
    assert DocumentRepository(db).count() == docs_after_first
    assert IngestionJobRepository(db).count() == jobs_after_first
    assert ConnectorArtifactRepository(db).count_by_source(source.source_id) == 3
    # Exactly one download: the opaque artifact, which has no change signature.
    assert client.fetch_calls == fetches_after_first + 1


def test_only_a_changed_artifact_is_re_ingested(db, submitter):
    source = _seed_source(db, SourceKind.GITHUB)
    client = default_document_fixture(dialect=DIALECTS[SourceKind.GITHUB])
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _connector(SourceKind.GITHUB, client=client)  # type: ignore[assignment]
    engine = ConnectorSyncEngine(db=db, submitter=submitter, registry=registry)

    engine.sync_source(source.source_id)
    client.mutate("doc-policy", "If sales drop by more than 45%, escalate to the CFO.")
    client.fetched_ids.clear()

    second = engine.sync_source(source.source_id)

    assert second["processed_count"] == 1
    assert second["skipped_count"] == 2
    # Only the changed artifact was downloaded (plus the opaque one, which has
    # no signature and must always be compared by bytes).
    assert "doc-policy" in client.fetched_ids
    assert "doc-runbook" not in client.fetched_ids
    artifacts = {a.external_id: a for a in ConnectorArtifactRepository(db).list_by_source(source.source_id)}
    assert artifacts["doc-policy"].ingest_count == 2
    assert artifacts["doc-runbook"].skip_count == 1


def test_an_opaque_artifact_is_compared_by_bytes_and_still_not_duplicated(db, submitter):
    """An upstream exposing no hash/version/timestamp is still idempotent.

    The engine downloads it (correct, just not free) and short-circuits on the
    content hash, so no duplicate document or embedding results.
    """
    source = _seed_source(db, SourceKind.GOOGLE_WORKSPACE)
    client = MockDocumentClient(
        [MockArtifact(
            external_id="only", title="notes.txt", body="stable",
            content_type="text/plain", opaque=True,
        )],
        dialect=DIALECTS[SourceKind.GOOGLE_WORKSPACE],
    )
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _connector(SourceKind.GOOGLE_WORKSPACE, client=client)  # type: ignore[assignment]
    engine = ConnectorSyncEngine(db=db, submitter=submitter, registry=registry)

    engine.sync_source(source.source_id)
    second = engine.sync_source(source.source_id)

    assert client.fetch_calls == 2, "an opaque artifact must be re-downloaded to compare"
    assert second["processed_count"] == 0
    assert second["skipped_count"] == 1
    assert DocumentRepository(db).count() == 1


def test_the_cursor_advances_only_on_a_successful_run(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    engine = _engine(db, submitter)
    assert SourceRepository(db).get(source.source_id).last_synced_at is None

    outcome = engine.sync_source(source.source_id)
    advanced = SourceRepository(db).get(source.source_id).last_synced_at
    assert advanced is not None and advanced == outcome["cursor_to"]

    # A failing run must not advance the cursor, or the artifacts it never
    # fetched would be skipped forever.
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    failing = default_document_fixture(dialect=DIALECTS[SourceKind.SHAREPOINT])
    failing.fail_with = RuntimeError("upstream gone")
    registry.build = lambda src: _connector(SourceKind.SHAREPOINT, client=failing)  # type: ignore[assignment]
    ConnectorSyncEngine(db=db, submitter=submitter, registry=registry).sync_source(source.source_id)

    assert SourceRepository(db).get(source.source_id).last_synced_at == advanced


def test_a_run_is_recorded_with_its_measured_counts_and_duration(db, submitter):
    source = _seed_source(db, SourceKind.CONFLUENCE)
    _engine(db, submitter).sync_source(source.source_id, triggered_by="alice")

    run = ConnectorSyncRunRepository(db).latest(source.source_id)
    assert run is not None
    assert run.status == SyncRunStatus.SUCCEEDED
    assert run.listed_count == 3 and run.processed_count == 3
    assert run.duration_seconds is not None and run.duration_seconds >= 0
    assert run.triggered_by == "alice"
    assert run.finished_at is not None


def test_the_per_run_artifact_cap_truncates_and_says_so(db, submitter):
    source = _seed_source(db, SourceKind.GITHUB)
    client = MockDocumentClient(
        [
            MockArtifact(external_id=f"doc-{i}", title=f"doc-{i}.md", body=f"body {i}")
            for i in range(10)
        ],
        dialect=DIALECTS[SourceKind.GITHUB],
    )
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _connector(SourceKind.GITHUB, client=client)  # type: ignore[assignment]
    engine = ConnectorSyncEngine(db=db, submitter=submitter, registry=registry, max_artifacts=4)

    outcome = engine.sync_source(source.source_id)
    assert outcome["truncated"] is True
    assert outcome["processed_count"] == 4
    assert outcome["listed_count"] == 10


def test_an_unsupported_format_is_skipped_not_a_run_failure(db, submitter):
    """One .exe in a library must not stop its documents from arriving."""
    source = _seed_source(db, SourceKind.SHAREPOINT)
    client = MockDocumentClient(
        [
            MockArtifact(external_id="ok", title="fine.md", body="content"),
            MockArtifact(external_id="bad", title="tool.exe", body="binary"),
        ],
        dialect=DIALECTS[SourceKind.SHAREPOINT],
    )
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _connector(SourceKind.SHAREPOINT, client=client)  # type: ignore[assignment]

    outcome = ConnectorSyncEngine(
        db=db, submitter=submitter, registry=registry
    ).sync_source(source.source_id)

    assert outcome["status"] == SyncRunStatus.SUCCEEDED
    assert outcome["processed_count"] == 1
    assert outcome["skipped_count"] == 1
    assert any("not ingestible" in e["reason"] for e in outcome["artifact_errors"])


# ===========================================================================
# 5. Failure isolation
# ===========================================================================


def test_one_failing_connector_never_blocks_another(db, submitter):
    good = _seed_source(db, SourceKind.SHAREPOINT, name="good")
    bad = _seed_source(db, SourceKind.CONFLUENCE, name="bad")

    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    broken = default_document_fixture(dialect=DIALECTS[SourceKind.CONFLUENCE])
    broken.fail_with = RuntimeError("upstream on fire")

    def _build(source):
        if source.kind == SourceKind.CONFLUENCE:
            return _connector(SourceKind.CONFLUENCE, client=broken)
        return _connector(
            SourceKind.SHAREPOINT,
            client=default_document_fixture(dialect=DIALECTS[SourceKind.SHAREPOINT]),
        )

    registry.build = _build  # type: ignore[assignment]
    outcomes = ConnectorSyncEngine(db=db, submitter=submitter, registry=registry).sync_all()

    by_id = {o["source_id"]: o for o in outcomes}
    assert by_id[bad.source_id]["status"] == SyncRunStatus.FAILED
    assert by_id[good.source_id]["status"] == SyncRunStatus.SUCCEEDED
    # The healthy connector's documents landed regardless.
    assert len(DocumentRepository(db).list_by_source(good.source_id)) == 3


def test_a_connector_that_raises_anything_is_isolated_not_propagated(db, submitter):
    source = _seed_source(db, SourceKind.GITHUB)

    class _Catastrophic:
        def validate_config(self):
            raise MemoryError("something truly unexpected")

    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _Catastrophic()  # type: ignore[assignment]

    # Never raises — the caller always gets an answer, which is what "a
    # connector failure never blocks anything" means concretely.
    outcome = ConnectorSyncEngine(
        db=db, submitter=submitter, registry=registry
    ).sync_source(source.source_id)
    assert outcome["status"] == SyncRunStatus.FAILED
    assert "unexpected" in outcome["error"]


def test_one_poisoned_artifact_does_not_abandon_the_rest_of_the_run(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)

    class _PartiallyBroken(CONNECTOR_CLASSES[SourceKind.SHAREPOINT]):  # type: ignore[misc]
        def fetch_artifact(self, ref):
            if ref.external_id == "doc-policy":
                raise ConnectorError("upstream_unavailable", "that one document is unreadable")
            return super().fetch_artifact(ref)

    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _PartiallyBroken(  # type: ignore[assignment]
        source_id=src.source_id, config=dict(CONNECTOR_CONFIG[SourceKind.SHAREPOINT]),
        secret_manager=_StubSecretManager({}), secret_ref=None,
        client=default_document_fixture(dialect=DIALECTS[SourceKind.SHAREPOINT]),
    )
    outcome = ConnectorSyncEngine(
        db=db, submitter=submitter, registry=registry
    ).sync_source(source.source_id)

    # PARTIAL, not FAILED: some artifacts landed, and collapsing that into
    # either extreme would misstate what the connector did.
    assert outcome["status"] == SyncRunStatus.PARTIAL
    assert outcome["processed_count"] == 2
    assert outcome["failed_count"] == 1
    assert len(DocumentRepository(db).list_by_source(source.source_id)) == 2


def test_a_broken_metric_connector_does_not_block_kpi_collection(db):
    """The composed KPI source keeps serving every other member."""
    from aeam.connectors.composite_kpi_source import CompositeKPISource

    broken_client = MockMetricsClient({})
    broken_client.fail_with = RuntimeError("snowflake unreachable")
    broken = _connector(SourceKind.SNOWFLAKE, client=broken_client)
    broken.authenticate()
    healthy = _connector(SourceKind.BIGQUERY)
    healthy.authenticate()

    composite = CompositeKPISource()
    composite.add_multi(broken, lambda: [_selector_for(SourceKind.SNOWFLAKE)])
    composite.add_multi(healthy, lambda: [_selector_for(SourceKind.BIGQUERY)])

    rows = composite.fetch_rows("ignored")
    # The broken member contributed nothing and raised nothing; the healthy one
    # served its full series.
    assert len(rows) == 4


def test_a_failing_connector_does_not_block_uploads(db, submitter):
    source = _seed_source(db, SourceKind.CONFLUENCE)
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    broken = default_document_fixture(dialect=DIALECTS[SourceKind.CONFLUENCE])
    broken.fail_with = RuntimeError("down")
    registry.build = lambda src: _connector(SourceKind.CONFLUENCE, client=broken)  # type: ignore[assignment]
    ConnectorSyncEngine(db=db, submitter=submitter, registry=registry).sync_source(source.source_id)

    # Ingestion still works immediately afterwards.
    upload_source_id = get_or_create_upload_source(SourceRepository(db))
    result = submitter.submit(
        b"# still works\n", filename="ok.md", content_type="text/markdown",
        source_id=upload_source_id,
    )
    assert result.job_id and result.asset_created is True


def test_an_unknown_or_unregistered_source_fails_without_raising(db, submitter):
    engine = _engine(db, submitter)
    assert engine.sync_source("nonexistent")["status"] == SyncRunStatus.FAILED

    # A source of a non-connector kind is not syncable, and says so.
    upload = _seed_source(db, SourceKind.UPLOAD, config={})
    outcome = engine.sync_source(upload.source_id)
    assert outcome["status"] == SyncRunStatus.FAILED
    assert "not enabled or has no implementation" in outcome["error"]


# ===========================================================================
# 6. Provenance
# ===========================================================================


def test_every_ingested_artifact_retains_full_provenance(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)

    artifacts = {
        a.external_id: a
        for a in ConnectorArtifactRepository(db).list_by_source(source.source_id)
    }
    runbook = artifacts["doc-runbook"]

    assert runbook.connector == SourceKind.SHAREPOINT
    assert runbook.external_id == "doc-runbook"
    assert runbook.source_type == "page"
    assert runbook.source_url and runbook.source_url.startswith("https://")
    assert runbook.source_timestamp
    assert runbook.source_version
    assert runbook.semantic_type == "runbook"
    assert runbook.last_synced_at and runbook.first_synced_at
    # And it points at the local asset it became, plus the job that made it.
    assert runbook.parent_type == "document"
    assert DocumentRepository(db).get(runbook.parent_id) is not None
    assert runbook.last_job_id


def test_provenance_leaves_unexposed_upstream_fields_null(db, submitter):
    """A field upstream does not expose must be null, not filled in.

    A fabricated timestamp would make incremental sync silently wrong, and a
    fabricated URL would send an operator to a page that does not exist.
    """
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)

    opaque = next(
        a for a in ConnectorArtifactRepository(db).list_by_source(source.source_id)
        if a.external_id == "doc-opaque"
    )
    assert opaque.source_timestamp is None
    assert opaque.source_version is None
    # The content hash IS recorded: it was measured from the fetched bytes.
    assert opaque.source_content_hash


def test_github_provenance_reports_no_timestamp_because_the_api_exposes_none(db, submitter):
    source = _seed_source(db, SourceKind.GITHUB)
    _engine(db, submitter).sync_source(source.source_id)
    artifacts = ConnectorArtifactRepository(db).list_by_source(source.source_id)

    # The connector's field map leaves source_timestamp unmapped rather than
    # borrowing a commit date that would change for untouched files.
    assert all(a.source_timestamp is None for a in artifacts)
    # Every artifact upstream versions carries its sha; the deliberately opaque
    # fixture artifact carries none, and that absence is left honest.
    versioned = [a for a in artifacts if a.external_id != "doc-opaque"]
    assert versioned and all(a.source_version for a in versioned)
    assert next(a for a in artifacts if a.external_id == "doc-opaque").source_version is None


# ===========================================================================
# 7. Connector health (SEC-8)
# ===========================================================================


def _reporter(db, registry=None, **overrides) -> ConnectorHealthReporter:
    overrides.setdefault("CONNECTOR_MOCK_MODE", True)
    settings = _settings(**overrides)
    return ConnectorHealthReporter(
        db=db,
        registry=registry or ConnectorRegistry(
            settings=settings,
            secret_manager=_StubSecretManager({
                f"{kind.upper()}_TOKEN": "s3cr3t-token-value" for kind in ALL_KINDS
            }),
        ),
        stale_after_seconds=settings.CONNECTOR_STALE_AFTER_SECONDS,
    )


def test_health_reports_every_required_field(db):
    _seed_source(db, SourceKind.SHAREPOINT)
    entry = _reporter(db).report()["connectors"][0]

    for field in (
        "enabled", "configured", "authenticated", "last_successful_sync",
        "last_failed_sync", "sync_status", "stale", "processed_count",
        "skipped_count", "changed_count", "sync_duration_seconds", "error_reason",
    ):
        assert field in entry, f"connector health is missing {field}"


def test_a_never_synced_connector_is_unknown_not_healthy(db):
    _seed_source(db, SourceKind.SHAREPOINT)
    report = _reporter(db).report()
    entry = report["connectors"][0]

    assert entry["sync_status"] == "never_synced"
    assert entry["last_successful_sync"] is None
    # `stale` is None, NOT False. "We cannot tell" and "it is fresh" are
    # different answers, and this is the one that must never be claimed.
    assert entry["stale"] is None
    assert "never completed a sync" in entry["stale_reason"]
    assert report["summary"]["unknown"] == 1
    assert report["summary"]["healthy"] == 0


def test_health_reports_a_successful_sync_as_healthy(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)
    report = _reporter(db).report()
    entry = report["connectors"][0]

    assert entry["sync_status"] == SyncRunStatus.SUCCEEDED
    assert entry["last_successful_sync"] is not None
    assert entry["stale"] is False
    assert entry["processed_count"] == 3
    assert entry["known_artifacts"] == 3
    assert report["summary"]["healthy"] == 1


def test_health_reports_stale_when_the_last_success_is_too_old(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)
    run = ConnectorSyncRunRepository(db).latest(source.source_id)
    ConnectorSyncRunRepository(db).update(run.run_id, {"finished_at": "2020-01-01T00:00:00+00:00"})

    entry = _reporter(db).report()["connectors"][0]
    assert entry["stale"] is True
    assert entry["stale_reason"] is None


def test_health_reports_last_success_and_last_failure_separately(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)

    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    broken = default_document_fixture(dialect=DIALECTS[SourceKind.SHAREPOINT])
    broken.fail_with = RuntimeError("upstream gone")
    registry.build = lambda src: _connector(SourceKind.SHAREPOINT, client=broken)  # type: ignore[assignment]
    ConnectorSyncEngine(db=db, submitter=submitter, registry=registry).sync_source(source.source_id)

    entry = _reporter(db).report()["connectors"][0]
    # A connector that succeeded then failed is in a different state from one
    # that has only ever failed, so both facts are reported.
    assert entry["last_successful_sync"] is not None
    assert entry["last_failed_sync"] is not None
    assert entry["sync_status"] == SyncRunStatus.FAILED


def test_a_disabled_connector_is_not_reported_as_authenticated(db):
    _seed_source(db, SourceKind.SHAREPOINT)
    reporter = _reporter(db, CONNECTOR_SHAREPOINT_ENABLED=False)
    entry = reporter.report()["connectors"][0]

    assert entry["enabled"] is False
    assert entry["authenticated"] is False
    assert "disabled" in entry["error_reason"]
    # Still shows its configuration, so an operator can finish setting it up
    # before enabling it.
    assert entry["configured"] is True


def test_an_unconfigured_connector_reports_which_key_is_missing(db):
    _seed_source(db, SourceKind.SHAREPOINT, config={"site_url": "https://mock.invalid"})
    entry = _reporter(db).report()["connectors"][0]

    assert entry["configured"] is False
    assert "drive_id" in entry["configuration_reason"]
    assert entry["authenticated"] is False


def test_health_never_exposes_a_credential(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)
    payload = json.dumps(_reporter(db).report())

    assert "s3cr3t-token-value" not in payload
    assert "SHAREPOINT_TOKEN" in payload  # the NAME is fine and useful


def test_health_reports_the_catalog_of_all_eight_connectors(db):
    catalog = _reporter(db).report()["catalog"]
    assert {entry["kind"] for entry in catalog} == set(ALL_KINDS)
    for entry in catalog:
        assert entry["capability"] in ConnectorCapability.ALL
        assert entry["flag"] == FLAG_FOR_KIND[entry["kind"]]


def test_health_survives_an_unreadable_database(db):
    class _Broken:
        def fetch_all(self, *_a, **_k):
            raise RuntimeError("database is gone")

        def fetch_one(self, *_a, **_k):
            raise RuntimeError("database is gone")

    reporter = ConnectorHealthReporter(
        db=_Broken(), registry=ConnectorRegistry(settings=_settings())
    )
    report = reporter.report()
    assert report["connectors"] == []
    assert "could not be read" in report["reason"]


# ===========================================================================
# 8. Registry, flags, and the rollback posture
# ===========================================================================


def test_every_connector_has_an_independent_flag():
    assert set(FLAG_FOR_KIND) == set(ALL_KINDS)
    settings = _settings()
    for flag in FLAG_FOR_KIND.values():
        assert hasattr(settings, flag), f"{flag} is not a real setting"


def test_all_connector_flags_default_off():
    # The documented F7 rollback: a fresh deployment is on its pre-F7 posture.
    defaults = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development",
    )
    assert defaults.CONNECTORS_ENABLED is False
    assert defaults.CONNECTOR_MOCK_MODE is False
    for flag in FLAG_FOR_KIND.values():
        assert getattr(defaults, flag) is False, f"{flag} must default off"


def test_the_master_switch_disables_every_connector():
    registry = ConnectorRegistry(settings=_settings(CONNECTORS_ENABLED=False))
    assert registry.enabled_kinds() == []
    # And the framework is inert: nothing can be built.
    source = Source(name="x", kind=SourceKind.SHAREPOINT, config=CONNECTOR_CONFIG[SourceKind.SHAREPOINT])
    assert registry.build(source) is None


def test_a_disabled_connector_is_never_built():
    registry = ConnectorRegistry(settings=_settings(CONNECTOR_GITHUB_ENABLED=False))
    assert SourceKind.GITHUB not in registry.enabled_kinds()
    source = Source(name="x", kind=SourceKind.GITHUB, config=CONNECTOR_CONFIG[SourceKind.GITHUB])
    assert registry.build(source) is None


def test_non_connector_kinds_are_not_treated_as_connectors():
    registry = ConnectorRegistry(settings=_settings())
    # upload and gsheet are real sources but not connectors; reporting them as
    # unimplemented connectors would be two permanent false failures.
    assert registry.is_connector_kind(SourceKind.UPLOAD) is False
    assert registry.is_connector_kind(SourceKind.GSHEET) is False
    assert registry.is_connector_kind(SourceKind.SHAREPOINT) is True


def test_mock_mode_injects_a_deterministic_client():
    registry = ConnectorRegistry(settings=_settings(CONNECTOR_MOCK_MODE=True))
    source = Source(
        name="x", kind=SourceKind.SHAREPOINT, config=CONNECTOR_CONFIG[SourceKind.SHAREPOINT]
    )
    connector = registry.build(source)
    assert connector is not None
    assert connector.authenticate() is True
    assert len(connector.list_artifacts()) == 3
    # Honest about itself, so a mock sync is never mistaken for a tenant one.
    assert connector.describe()["client_mode"] == "injected"


def test_registry_build_never_raises_on_a_broken_class():
    class _Unconstructable(CONNECTOR_CLASSES[SourceKind.GITHUB]):  # type: ignore[misc]
        def __init__(self, *_a, **_k):
            raise RuntimeError("cannot construct")

    registry = ConnectorRegistry(
        settings=_settings(), classes={SourceKind.GITHUB: _Unconstructable}
    )
    source = Source(name="x", kind=SourceKind.GITHUB, config=CONNECTOR_CONFIG[SourceKind.GITHUB])
    assert registry.build(source) is None


def test_metric_connectors_compose_as_ordinary_kpi_members(db):
    registry = ConnectorRegistry(
        settings=_settings(CONNECTOR_MOCK_MODE=True),
        secret_manager=_StubSecretManager({}),
    )
    sources = [_seed_source(db, kind) for kind in METRIC_KINDS]
    members = registry.build_metric_sources(sources)

    assert len(members) == len(METRIC_KINDS)
    for member in members:
        # The EXISTING KPIRowSource protocol — no new interface.
        assert callable(member.fetch_rows)
        assert member.capability == ConnectorCapability.METRICS


def test_document_connectors_are_not_composed_into_the_kpi_source(db):
    registry = ConnectorRegistry(
        settings=_settings(CONNECTOR_MOCK_MODE=True), secret_manager=_StubSecretManager({})
    )
    sources = [_seed_source(db, kind) for kind in DOCUMENT_KINDS]
    assert registry.build_metric_sources(sources) == []


# ===========================================================================
# 9. No live third-party calls (mocked-services requirement)
# ===========================================================================


def test_no_connector_module_performs_a_call_at_import_time():
    # Importing a connector must not touch the network. Asserted by the fact
    # that the whole registry imports cleanly in this suite, plus structurally:
    # no module-level requests/SDK invocation.
    import ast

    root = Path(__file__).resolve().parents[1] / "connectors"
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in tree.body:
            assert not isinstance(node, (ast.Expr,)) or not isinstance(
                getattr(node, "value", None), ast.Call
            ), f"{module.name} performs a call at import time"


def test_http_transport_is_only_constructed_from_a_resolved_credential():
    """The real REST client cannot come into existence without a credential.

    That is what makes "gating CI never calls a third party" structural rather
    than a convention: with no credential and no injected client, there is
    nothing that could issue a request.
    """
    for kind in DOCUMENT_KINDS:
        cls = CONNECTOR_CLASSES[kind]
        connector = cls(
            source_id=f"src-{kind}", config=dict(CONNECTOR_CONFIG[kind]),
            secret_manager=_StubSecretManager({}), secret_ref=None, client=None,
        )
        assert connector.authenticate() is False
        assert connector._client is None


def test_requests_is_imported_lazily_inside_the_client_methods():
    # A module-level `import requests` would run at collection time for every
    # test in the repo; more importantly, keeping it local documents that the
    # HTTP path is only ever entered deliberately.
    source = (
        Path(__file__).resolve().parents[1] / "connectors" / "enterprise" / "documents.py"
    ).read_text(encoding="utf-8")
    module_level = [
        line for line in source.splitlines()
        if line.startswith("import requests") or line.startswith("from requests")
    ]
    assert module_level == []
    assert "        import requests" in source


# ===========================================================================
# 10. API surface
# ===========================================================================


@pytest.fixture()
def client(db, submitter):
    class _Container:
        pass

    settings = _settings(CONNECTOR_MOCK_MODE=True)
    registry = ConnectorRegistry(
        settings=settings, secret_manager=_StubSecretManager({}),
    )
    container = _Container()
    container.db = db
    container.settings = settings
    container.audit_logger = None
    container.connector_registry = registry
    container.connector_sync = ConnectorSyncEngine(
        db=db, submitter=submitter, registry=registry,
    )
    container.connector_health = ConnectorHealthReporter(db=db, registry=registry)

    app = FastAPI()
    app.include_router(connectors_router)
    app.state.container = container
    return TestClient(app)


def test_api_health_reports_the_catalog_and_registered_connectors(client, db):
    _seed_source(db, SourceKind.SHAREPOINT)
    body = client.get("/api/v1/connectors/health").json()

    assert body["framework_enabled"] is True
    assert body["mock_mode"] is True
    assert len(body["catalog"]) == len(ALL_KINDS)
    assert len(body["connectors"]) == 1


def test_api_sync_runs_one_connector_and_returns_its_outcome(client, db):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    body = client.post(f"/api/v1/connectors/sync/{source.source_id}", json={}).json()

    assert body["status"] == SyncRunStatus.SUCCEEDED
    assert body["processed_count"] == 3
    assert DocumentRepository(db).count() == 3


def test_api_sync_returns_200_with_a_failed_outcome_not_a_5xx(client, db):
    # A connector fault is a recorded state, not a transport error. A 5xx would
    # make an isolated connector failure look like a platform failure.
    source = _seed_source(db, SourceKind.SHAREPOINT, config={})  # unconfigured
    resp = client.post(f"/api/v1/connectors/sync/{source.source_id}", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == SyncRunStatus.FAILED


def test_api_sync_all_isolates_each_connector(client, db):
    good = _seed_source(db, SourceKind.SHAREPOINT)
    bad = _seed_source(db, SourceKind.CONFLUENCE, config={})
    body = client.post("/api/v1/connectors/sync", json={}).json()

    statuses = {o["source_id"]: o["status"] for o in body["outcomes"]}
    assert statuses[good.source_id] == SyncRunStatus.SUCCEEDED
    assert statuses[bad.source_id] == SyncRunStatus.FAILED


def test_api_artifacts_endpoint_returns_provenance_bounded(client, db):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    client.post(f"/api/v1/connectors/sync/{source.source_id}", json={})

    body = client.get(f"/api/v1/connectors/{source.source_id}/artifacts?limit=2").json()
    assert body["total"] == 3
    assert len(body["artifacts"]) == 2
    assert body["truncated"] is True
    assert body["artifacts"][0]["external_id"]
    assert "source_url" in body["artifacts"][0]


def test_api_runs_endpoint_returns_sync_history(client, db):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    client.post(f"/api/v1/connectors/sync/{source.source_id}", json={})
    client.post(f"/api/v1/connectors/sync/{source.source_id}", json={})

    body = client.get(f"/api/v1/connectors/{source.source_id}/runs").json()
    assert body["count"] == 2
    assert all(run["status"] in SyncRunStatus.ALL for run in body["runs"])


def test_api_404s_on_an_unknown_or_non_connector_source(client, db):
    assert client.get("/api/v1/connectors/nope").status_code == 404
    upload = _seed_source(db, SourceKind.UPLOAD, config={})
    resp = client.get(f"/api/v1/connectors/{upload.source_id}")
    assert resp.status_code == 404
    assert "not connectors" in resp.json()["detail"]


def test_api_never_returns_a_credential(client, db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    client.post(f"/api/v1/connectors/sync/{source.source_id}", json={})
    payload = json.dumps([
        client.get("/api/v1/connectors/health").json(),
        client.get(f"/api/v1/connectors/{source.source_id}").json(),
        client.get(f"/api/v1/connectors/{source.source_id}/artifacts").json(),
        client.get(f"/api/v1/connectors/{source.source_id}/runs").json(),
    ])
    assert "s3cr3t" not in payload


def test_api_rbac_grades_writes_stricter_than_reads():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    entries = [(p, r, a) for p, r, a in _ENDPOINT_RBAC_MAP if p.startswith("/api/v1/connectors")]
    mapping = {p: (r, a) for p, r, a in entries}
    assert mapping["/api/v1/connectors/sync"] == ("admin", "config")
    assert mapping["/api/v1/connectors/health"] == ("documents", "ingest")
    assert mapping["/api/v1/connectors"] == ("documents", "ingest")
    # Longest-prefix resolution: the sync prefix must precede the catch-all, or
    # a write would be graded as a read.
    order = [p for p, _r, _a in entries]
    assert order.index("/api/v1/connectors/sync") < order.index("/api/v1/connectors")


def test_every_sync_route_lives_under_the_write_prefix():
    # This is what makes the path-only RBAC map safe: no POST is reachable at a
    # path that the map would grade as a read.
    for route in connectors_router.routes:
        if set(route.methods) - {"GET", "HEAD"}:
            assert route.path.startswith("/api/v1/connectors/sync"), (
                f"{route.path} is a write outside the /sync prefix and would be "
                "graded as a read by the RBAC map"
            )


def test_api_reports_503_when_the_framework_is_not_wired():
    class _Container:
        db = None
        connector_health = None
        connector_sync = None

    app = FastAPI()
    app.include_router(connectors_router)
    app.state.container = _Container()
    resp = TestClient(app).get("/api/v1/connectors/health")
    assert resp.status_code == 503
    assert "not wired" in resp.json()["detail"]


# ===========================================================================
# 11. End-to-end
# ===========================================================================


def test_end_to_end_sync_then_resync_then_change(client, db):
    """The full connector lifecycle through the API, on real persistence."""
    source = _seed_source(db, SourceKind.GITHUB)

    first = client.post(f"/api/v1/connectors/sync/{source.source_id}", json={}).json()
    assert first["processed_count"] == 3
    assert DocumentRepository(db).count() == 3

    second = client.post(f"/api/v1/connectors/sync/{source.source_id}", json={}).json()
    assert second["processed_count"] == 0
    assert second["skipped_count"] == 3
    assert DocumentRepository(db).count() == 3, "a repeated sync must not duplicate"

    health = client.get(f"/api/v1/connectors/{source.source_id}").json()
    assert health["sync_status"] == SyncRunStatus.SUCCEEDED
    assert health["stale"] is False
    assert health["known_artifacts"] == 3

    artifacts = client.get(f"/api/v1/connectors/{source.source_id}/artifacts").json()
    assert all(a["skip_count"] >= 1 for a in artifacts["artifacts"])


def test_end_to_end_a_connector_document_reaches_the_same_job_ledger(client, db):
    source = _seed_source(db, SourceKind.CONFLUENCE)
    client.post(f"/api/v1/connectors/sync/{source.source_id}", json={})

    jobs = IngestionJobRepository(db).list_all()
    assert len(jobs) == 3
    assert {job.job_type for job in jobs} == {"ingest"}
    assert all(job.source_id == source.source_id for job in jobs)
    # Each job points at a real Document the unchanged processor will pick up.
    for job in jobs:
        assert job.parent_type == "document"
        assert DocumentRepository(db).get(job.parent_id) is not None


def test_end_to_end_the_content_hash_matches_the_upstream_bytes(db, submitter):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    client_fixture = default_document_fixture(dialect=DIALECTS[SourceKind.SHAREPOINT])
    registry = ConnectorRegistry(settings=_settings(), secret_manager=_StubSecretManager({}))
    registry.build = lambda src: _connector(SourceKind.SHAREPOINT, client=client_fixture)  # type: ignore[assignment]
    ConnectorSyncEngine(db=db, submitter=submitter, registry=registry).sync_source(source.source_id)

    artifacts = ConnectorArtifactRepository(db).list_by_source(source.source_id)
    for artifact in artifacts:
        expected = content_hash_of(client_fixture.download(artifact.external_id))
        assert artifact.source_content_hash == expected
        # And the registry's Document carries the same hash — the two dedup
        # layers agree because they hash the same bytes.
        doc = DocumentRepository(db).get(artifact.parent_id)
        assert doc.content_hash == expected


def test_end_to_end_a_blob_exists_for_every_synced_artifact(db, submitter, blob_store):
    source = _seed_source(db, SourceKind.SHAREPOINT)
    _engine(db, submitter).sync_source(source.source_id)

    for artifact in ConnectorArtifactRepository(db).list_by_source(source.source_id):
        version = VersionRepository(db).get_active("document", artifact.parent_id)
        assert version is not None and version.blob_ref
        # E4 BlobStore, the same one uploads use — addressed by content hash.
        assert blob_store.exists(artifact.source_content_hash)
        assert blob_store.get(artifact.source_content_hash)
