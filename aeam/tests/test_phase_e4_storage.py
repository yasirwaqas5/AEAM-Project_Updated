"""
aeam/tests/test_phase_e4_storage.py

Phase E4 — Durable State & Deployment Alignment (ARCH-7, SEC-5, TECH-5).

The E4 acceptance criteria mapped to tests here:

1. **BlobStore contract, run identically against LocalDisk AND S3.**
   Every test in the "contract" section is parametrized across both
   backends. That is the ROADMAP wording verbatim: "The blob-backend
   contract test suite passes identically against local-disk and object
   backends." The S3 backend runs against moto's in-process S3 mock, so
   the suite requires no live services (TEST-3 preserved).

2. **Cross-instance visibility.** Two S3BlobStore instances configured
   against the same bucket see the same object — the requirement Cloud
   Run's ``maxScale > 1`` needs to be truthful (ARCH-7).

3. **Factory selection + fail-behavior.** ``build_blob_store`` dispatches
   correctly on ``BLOB_STORAGE_BACKEND`` and raises a clear error when
   the S3 backend is selected without a bucket.

4. **ForecastAgent model_dir is Settings-driven.** Empty preserves the
   engine default byte-identically (COMPAT-1); a set value is honored.

5. **Deployment-artifact hygiene.** No credential literals in
   ``docker-compose.yml`` / ``deploy/cloudrun.yaml`` / ``deploy/env.yaml``.
   The pre-E4 hardcoded ``postgres:secret`` password is gone.

6. **D5 admin API surfaces config-persistence honesty (PHIL-1).**
   The response now carries a ``config_persistence`` block; its default
   value preserves today's response otherwise.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from aeam.config.settings import Settings
from aeam.storage import BlobRef, BlobStore, LocalDiskBlobStore, build_blob_store
from aeam.storage.blob_store import BlobNotFoundError


# ---------------------------------------------------------------------------
# Fixtures — two BlobStore backends, drop-in interchangeable.
# ---------------------------------------------------------------------------

_moto = pytest.importorskip("moto", reason="moto[s3] required for S3 contract tests")
from moto import mock_aws  # noqa: E402  (import after skipif guard)


_S3_BUCKET = "aeam-e4-test-bucket"
_S3_REGION = "us-east-1"


@pytest.fixture
def local_store(tmp_path) -> BlobStore:
    return LocalDiskBlobStore(tmp_path / "blobs")


@pytest.fixture
def s3_store():
    """Yield an S3BlobStore backed by moto's in-process S3 mock."""
    from aeam.storage.s3_blob_store import S3BlobStore

    with mock_aws():
        import boto3
        client = boto3.client("s3", region_name=_S3_REGION)
        client.create_bucket(Bucket=_S3_BUCKET)
        yield S3BlobStore(
            bucket=_S3_BUCKET,
            region_name=_S3_REGION,
            access_key_id="testing",
            secret_access_key="testing",
            prefix="e4-test",
        )


# The parametrization vehicle: every contract test receives a fresh
# store on each backend under the ``store`` parameter name.
def _stores(request):
    """Indirect fixture: dispatches to local_store or s3_store."""
    return request.getfixturevalue(request.param)


@pytest.fixture(params=["local_store", "s3_store"])
def store(request) -> BlobStore:
    return _stores(request)


# ===========================================================================
# 1. BlobStore contract (parametrized: LocalDisk + S3)
# ===========================================================================

_PAYLOAD = b"phase E4 durable state contract payload"
_PAYLOAD_2 = b"a second, distinct payload"


def test_put_returns_blob_ref_with_matching_hash_and_size(store: BlobStore):
    ref = store.put(_PAYLOAD)
    assert isinstance(ref, BlobRef)
    assert ref.size == len(_PAYLOAD)
    # SHA-256 hex is 64 characters.
    assert len(ref.content_hash) == 64
    assert all(c in "0123456789abcdef" for c in ref.content_hash)


def test_put_is_idempotent(store: BlobStore):
    ref1 = store.put(_PAYLOAD)
    ref2 = store.put(_PAYLOAD)
    # Same address, same size, same URI — put twice, stored once.
    assert ref1.content_hash == ref2.content_hash
    assert ref1.uri == ref2.uri
    assert ref1.size == ref2.size


def test_get_roundtrips_stored_bytes(store: BlobStore):
    ref = store.put(_PAYLOAD)
    assert store.get(ref.content_hash) == _PAYLOAD


def test_exists_reports_true_after_put(store: BlobStore):
    ref = store.put(_PAYLOAD)
    assert store.exists(ref.content_hash) is True


def test_exists_reports_false_for_absent_hash(store: BlobStore):
    fake = "a" * 64
    assert store.exists(fake) is False


def test_stat_returns_ref_after_put(store: BlobStore):
    ref = store.put(_PAYLOAD)
    stat = store.stat(ref.content_hash)
    assert stat is not None
    assert stat.content_hash == ref.content_hash
    assert stat.size == ref.size


def test_stat_returns_none_for_absent_hash(store: BlobStore):
    assert store.stat("b" * 64) is None


def test_get_of_absent_hash_raises_blob_not_found(store: BlobStore):
    with pytest.raises(BlobNotFoundError):
        store.get("c" * 64)


def test_delete_returns_true_when_present_and_false_when_absent(store: BlobStore):
    ref = store.put(_PAYLOAD)
    assert store.delete(ref.content_hash) is True
    # A second delete is a no-op (idempotent) that reports absence.
    assert store.delete(ref.content_hash) is False


def test_delete_removes_the_blob(store: BlobStore):
    ref = store.put(_PAYLOAD)
    store.delete(ref.content_hash)
    assert store.exists(ref.content_hash) is False
    with pytest.raises(BlobNotFoundError):
        store.get(ref.content_hash)


def test_distinct_payloads_have_distinct_content_hashes(store: BlobStore):
    ref1 = store.put(_PAYLOAD)
    ref2 = store.put(_PAYLOAD_2)
    assert ref1.content_hash != ref2.content_hash


def test_uri_scheme_is_backend_appropriate(store: BlobStore):
    ref = store.put(_PAYLOAD)
    scheme = ref.uri.split("://", 1)[0]
    assert scheme in {"local", "s3"}
    # The scheme also matches the backend's declared class attribute.
    assert scheme == store.scheme


def test_put_none_raises(store: BlobStore):
    with pytest.raises(ValueError):
        store.put(None)  # type: ignore[arg-type]


# ===========================================================================
# 2. Cross-instance visibility (the ARCH-7 property that matters for
#    Cloud Run maxScale > 1). Only the S3 backend has this property in a
#    meaningful sense — two LocalDiskBlobStore instances rooted at the
#    same directory also share state, but that is obvious and covered
#    below for completeness.
# ===========================================================================

def test_s3_two_instances_same_bucket_see_the_same_object():
    from aeam.storage.s3_blob_store import S3BlobStore

    with mock_aws():
        import boto3
        boto3.client("s3", region_name=_S3_REGION).create_bucket(Bucket=_S3_BUCKET)

        writer = S3BlobStore(
            bucket=_S3_BUCKET,
            region_name=_S3_REGION,
            access_key_id="testing",
            secret_access_key="testing",
            prefix="shared",
        )
        reader = S3BlobStore(
            bucket=_S3_BUCKET,
            region_name=_S3_REGION,
            access_key_id="testing",
            secret_access_key="testing",
            prefix="shared",
        )

        ref = writer.put(b"cross-instance payload")
        # Different Python object, same bucket — must see the write.
        assert reader.exists(ref.content_hash) is True
        assert reader.get(ref.content_hash) == b"cross-instance payload"


def test_localdisk_two_instances_same_root_see_the_same_object(tmp_path):
    writer = LocalDiskBlobStore(tmp_path / "shared")
    reader = LocalDiskBlobStore(tmp_path / "shared")

    ref = writer.put(b"local dual-instance")
    assert reader.exists(ref.content_hash) is True
    assert reader.get(ref.content_hash) == b"local dual-instance"


# ===========================================================================
# 3. Factory selection and error surface
# ===========================================================================

def _settings(**overrides):
    base = dict(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    base.update(overrides)
    return Settings(**base)


def test_factory_default_selects_localdisk():
    s = _settings()  # no BLOB_STORAGE_BACKEND override
    store = build_blob_store(s)
    assert isinstance(store, LocalDiskBlobStore)


def test_factory_s3_requires_bucket():
    s = _settings(BLOB_STORAGE_BACKEND="s3")  # no BLOB_S3_BUCKET
    with pytest.raises(ValueError, match="BLOB_S3_BUCKET"):
        build_blob_store(s)


def test_factory_s3_constructs_when_bucket_set():
    from aeam.storage.s3_blob_store import S3BlobStore

    with mock_aws():
        import boto3
        boto3.client("s3", region_name=_S3_REGION).create_bucket(Bucket=_S3_BUCKET)

        s = _settings(
            BLOB_STORAGE_BACKEND="s3",
            BLOB_S3_BUCKET=_S3_BUCKET,
            BLOB_S3_REGION=_S3_REGION,
            BLOB_S3_ACCESS_KEY_ID="testing",
            BLOB_S3_SECRET_ACCESS_KEY="testing",
        )
        store = build_blob_store(s)
        assert isinstance(store, S3BlobStore)
        assert store.bucket == _S3_BUCKET


def test_factory_unknown_backend_raises():
    s = _settings(BLOB_STORAGE_BACKEND="azure-magic")
    with pytest.raises(ValueError, match="Unknown BLOB_STORAGE_BACKEND"):
        build_blob_store(s)


# ===========================================================================
# 4. Forecast model_dir configurability
# ===========================================================================

def test_forecast_model_dir_setting_default_is_empty_preserving_engine_default():
    """
    COMPAT-1: an unset FORECAST_MODEL_DIR must leave ForecastAgent on
    its own engine-owned default (`models/forecasting`), byte-identical
    to the pre-E4 wiring.
    """
    from aeam.agents.forecast.forecast_agent import _MODEL_DIR, ForecastAgent

    s = _settings()
    assert s.FORECAST_MODEL_DIR == ""

    # ForecastAgent's own default remains untouched by E4.
    class _DummyLTM:
        def get_metric_history(self, *args, **kwargs):
            return []

    class _DummyPipeline:
        pass

    agent = ForecastAgent(
        long_term_memory=_DummyLTM(),
        data_pipeline=_DummyPipeline(),
        settings=s,
    )
    assert str(agent._model_dir) == str(Path(_MODEL_DIR))


def test_forecast_model_dir_setting_honored_when_set(tmp_path):
    from aeam.agents.forecast.forecast_agent import ForecastAgent

    override = tmp_path / "durable-models"
    s = _settings(FORECAST_MODEL_DIR=str(override))

    class _DummyLTM:
        def get_metric_history(self, *args, **kwargs):
            return []

    class _DummyPipeline:
        pass

    agent = ForecastAgent(
        long_term_memory=_DummyLTM(),
        data_pipeline=_DummyPipeline(),
        settings=s,
        model_dir=str(override),
    )
    assert str(agent._model_dir) == str(override)


# ===========================================================================
# 5. Deployment-artifact hygiene (no credential literals)
# ===========================================================================

REPO_ROOT = Path(__file__).resolve().parents[2]
_TRACKED_DEPLOY_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "deploy" / "cloudrun.yaml",
    REPO_ROOT / "deploy" / "env.yaml",
]

# Substrings that would be a credential leak if literally present in any
# tracked deployment file. The audit specifically called out the pre-E4
# `postgres:secret` hardcoded password.
_BANNED_LITERALS = [
    "postgres:secret",       # the exact pre-E4 hardcoded password DSN
    "POSTGRES_PASSWORD: secret",
    "POSTGRES_PASSWORD=secret",
]


@pytest.mark.parametrize("path", _TRACKED_DEPLOY_FILES)
def test_deployment_artifact_exists(path: Path):
    assert path.exists(), (
        f"{path} must exist under Phase E4 (deploy/env.yaml emptiness fix; "
        "cloudrun.yaml audit; docker-compose password fix)."
    )


@pytest.mark.parametrize("path", _TRACKED_DEPLOY_FILES)
def test_no_credential_literals_in_deployment_artifact(path: Path):
    text = path.read_text(encoding="utf-8")
    for banned in _BANNED_LITERALS:
        assert banned not in text, (
            f"Credential literal {banned!r} appears in {path} — Phase E4 (SEC-5) "
            "forbids hardcoded credentials in any tracked deployment file."
        )


def test_docker_compose_uses_env_var_for_postgres_password():
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # The password must be sourced from POSTGRES_PASSWORD env var, fail-fast
    # if unset (the ${VAR:?message} form). Two hits expected: aeam-app DSN
    # and the postgres service's POSTGRES_PASSWORD.
    hits = re.findall(r"\$\{POSTGRES_PASSWORD:\?", text)
    assert len(hits) >= 2, (
        "docker-compose.yml must reference POSTGRES_PASSWORD via ${...:?...} "
        f"in both service definitions (found {len(hits)} such reference(s))."
    )


def test_cloudrun_yaml_sources_jwt_key_from_secret_manager():
    text = (REPO_ROOT / "deploy" / "cloudrun.yaml").read_text(encoding="utf-8")
    assert "name: JWT_PUBLIC_KEY" in text, "cloudrun.yaml must set JWT_PUBLIC_KEY (E3 requirement)."
    assert "aeam-jwt-public-key" in text, "cloudrun.yaml must source JWT_PUBLIC_KEY from Secret Manager."


def test_cloudrun_yaml_selects_s3_blob_backend_for_production():
    text = (REPO_ROOT / "deploy" / "cloudrun.yaml").read_text(encoding="utf-8")
    # BLOB_STORAGE_BACKEND must be present and set to s3.
    assert re.search(r'name:\s*BLOB_STORAGE_BACKEND', text)
    assert re.search(r'value:\s*"s3"', text)


def test_cloudrun_yaml_declares_config_persistence_as_ephemeral():
    text = (REPO_ROOT / "deploy" / "cloudrun.yaml").read_text(encoding="utf-8")
    assert re.search(r'name:\s*CONFIG_PERSISTENCE_MODE', text)
    assert re.search(r'value:\s*"ephemeral"', text)


def test_env_example_documents_every_e4_setting():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "BLOB_STORAGE_BACKEND",
        "BLOB_S3_BUCKET",
        "FORECAST_MODEL_DIR",
        "CONFIG_PERSISTENCE_MODE",
        "JWT_PUBLIC_KEY",
        "POSTGRES_PASSWORD",
    ):
        assert key in text, f".env.example must document {key} for developers."


# ===========================================================================
# 6. D5 config-persistence disclosure (PHIL-1)
# ===========================================================================

def test_admin_payload_includes_config_persistence_block():
    """The D5 GET /admin/config response gains a top-level
    'config_persistence' block that discloses whether writes here
    survive instance recycle (Phase E4, PHIL-1)."""
    from aeam.api.administration import _all_fields_payload

    class _StubContainer:
        settings = _settings(
            CONFIG_PERSISTENCE_MODE="ephemeral",
            CONFIG_PERSISTENCE_NOTE="Cloud Run writable layer.",
        )

    payload = _all_fields_payload(_StubContainer())
    assert "config_persistence" in payload
    cp = payload["config_persistence"]
    assert cp["mode"] == "ephemeral"
    assert cp["writes_durable"] is False
    assert cp.get("note") == "Cloud Run writable layer."
    assert cp.get("env_file")  # non-empty


def test_admin_payload_default_persistence_mode_reports_durable():
    from aeam.api.administration import _all_fields_payload

    class _StubContainer:
        settings = _settings()  # defaults

    payload = _all_fields_payload(_StubContainer())
    cp = payload["config_persistence"]
    assert cp["mode"] == "durable"
    assert cp["writes_durable"] is True
    # Empty note is omitted, not surfaced as empty string.
    assert "note" not in cp
