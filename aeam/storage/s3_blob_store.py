"""
aeam/storage/s3_blob_store.py

Object-store implementation of the :class:`~aeam.storage.blob_store.BlobStore`
ABC for Phase E4 (ARCH-7 / SEC-5 / TECH-1 / TECH-5).

The abstract ``BlobStore`` in ``aeam/storage/blob_store.py`` was explicitly
designed to anticipate S3/Azure/GCS — its docstring says so, and every
caller depends only on the ABC. This module is the S3-family concrete
backend: it talks to any S3-compatible endpoint using the standard
``boto3`` client, which covers:

* AWS S3           (native)
* MinIO            (self-hosted or in-CI)
* Cloudflare R2    (S3-compatible)
* Wasabi           (S3-compatible)
* GCS              (via the enterprise HMAC / S3 interoperability API)

Behavior — identical to :class:`~aeam.storage.blob_store.LocalDiskBlobStore`
by construction, since both honor the same ABC:

* Content-addressed: objects are keyed by their SHA-256 hash.
* :meth:`put` is idempotent (identical bytes → same object, no rewrite).
* Write-once: stored content is never mutated.
* Cross-instance visibility: two ``S3BlobStore`` instances configured
  against the same bucket see the same objects — the requirement Cloud
  Run's ``maxScale > 1`` needs to be truthful (ARCH-7).

TECH-5 justification: this backend exists solely to retire a constitutional
violation — durable storage on ephemeral compute (Cloud Run's local disk
is scratch). It adds no capability beyond what the ABC already promises.

Credential handling: any credential value passed here is resolved
upstream through :class:`~aeam.integrations.secret_manager.SecretManager`
by :func:`aeam.storage.factory.build_blob_store`. This module never reads
environment variables or configuration files directly, and never writes
credentials to logs.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.storage.blob_store import (
    BlobNotFoundError,
    BlobRef,
    BlobStore,
    compute_content_hash,
)

logger = logging.getLogger(__name__)


def _import_boto3():
    """Lazy import so the module remains importable when boto3 is absent
    (e.g. in a build environment that never selects the S3 backend)."""
    try:
        import boto3  # type: ignore[import-not-found]
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "S3BlobStore requires boto3. Install it via `pip install boto3` "
            "or select BLOB_STORAGE_BACKEND=local."
        ) from exc
    return boto3, ClientError


class S3BlobStore(BlobStore):
    """
    S3-compatible content-addressable blob store.

    Objects are stored at ``<prefix>/<h0h1>/<h2h3>/<hash>`` — the same
    two-level fan-out as :class:`LocalDiskBlobStore`, so keys stay
    balanced across S3's internal partitioning. Content hashing +
    idempotency + URI addressing are inherited from the ABC contract.

    Args:
        bucket:       S3 bucket name (must already exist and be writable
                      by the configured credentials).
        endpoint_url: Custom S3 endpoint (MinIO, R2, GCS HMAC). ``None``
                      routes to AWS's regional endpoint.
        region_name:  AWS region for endpoint resolution. Optional for
                      non-AWS endpoints.
        access_key_id, secret_access_key: Credentials passed through
                      from SecretManager. ``None`` on either falls back
                      to boto3's default provider chain (IMDS, IAM role,
                      shared credentials file, environment variables) —
                      the standard AWS SDK behavior. Never accessed
                      directly by any caller.
        prefix:       Key prefix inside the bucket. Empty by default;
                      set this when sharing a bucket across environments.

    Raises:
        ValueError: If ``bucket`` is empty or whitespace-only.
    """

    scheme = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        prefix: str = "",
    ) -> None:
        if not bucket or not bucket.strip():
            raise ValueError("bucket must be a non-empty string.")

        boto3, ClientError = _import_boto3()
        self._bucket: str = bucket.strip()
        self._prefix: str = prefix.strip("/")
        self._ClientError = ClientError

        client_kwargs: dict[str, Any] = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if region_name:
            client_kwargs["region_name"] = region_name
        if access_key_id and secret_access_key:
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = secret_access_key

        self._client = boto3.client("s3", **client_kwargs)

        logger.info(
            "S3BlobStore initialised | bucket=%s | endpoint=%s | prefix=%s",
            self._bucket,
            endpoint_url or "<aws-default>",
            self._prefix or "<none>",
        )

    # ------------------------------------------------------------------
    # BlobStore contract
    # ------------------------------------------------------------------

    def put(self, data: bytes, *, content_type: str | None = None) -> BlobRef:
        if data is None:
            raise ValueError("data must not be None.")

        content_hash = compute_content_hash(data)
        key = self._key_for(content_hash)

        # Idempotency: HEAD first. Cheaper than re-PUT on the common
        # duplicate-upload path (Phase B1.1 dedup keeps duplicates
        # bounded even in a fresh bucket).
        if self._object_exists(key):
            logger.debug("S3BlobStore.put | idempotent hit | hash=%s", content_hash)
            return BlobRef(content_hash, len(data), self.uri_for(content_hash))

        put_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            put_kwargs["ContentType"] = content_type

        self._client.put_object(**put_kwargs)
        logger.debug(
            "S3BlobStore.put | stored | hash=%s | size=%d | bucket=%s",
            content_hash, len(data), self._bucket,
        )
        return BlobRef(content_hash, len(data), self.uri_for(content_hash))

    def get(self, content_hash: str) -> bytes:
        self._validate_hash(content_hash)
        key = self._key_for(content_hash)
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404"}:
                raise BlobNotFoundError(content_hash) from exc
            raise
        return resp["Body"].read()

    def exists(self, content_hash: str) -> bool:
        self._validate_hash(content_hash)
        return self._object_exists(self._key_for(content_hash))

    def delete(self, content_hash: str) -> bool:
        self._validate_hash(content_hash)
        key = self._key_for(content_hash)
        if not self._object_exists(key):
            return False
        self._client.delete_object(Bucket=self._bucket, Key=key)
        return True

    def stat(self, content_hash: str) -> BlobRef | None:
        self._validate_hash(content_hash)
        key = self._key_for(content_hash)
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=key)
        except self._ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise
        size = int(resp.get("ContentLength", 0))
        return BlobRef(content_hash, size, self.uri_for(content_hash))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _key_for(self, content_hash: str) -> str:
        h = content_hash.strip().lower()
        fan_out = f"{h[:2]}/{h[2:4]}/{h}"
        return f"{self._prefix}/{fan_out}" if self._prefix else fan_out

    def _object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except self._ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise

    @staticmethod
    def _validate_hash(content_hash: str) -> None:
        h = str(content_hash).strip().lower()
        if len(h) < 4 or not all(c in "0123456789abcdef" for c in h):
            raise ValueError(f"invalid content_hash: {content_hash!r}")

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def __repr__(self) -> str:
        return f"S3BlobStore(bucket={self._bucket!r}, prefix={self._prefix!r})"
