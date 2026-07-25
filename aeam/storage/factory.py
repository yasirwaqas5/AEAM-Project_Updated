"""
aeam/storage/factory.py

Phase E4 (ARCH-7 / TECH-5) — BlobStore backend selector.

The single place that decides which concrete
:class:`~aeam.storage.blob_store.BlobStore` implementation the platform
runs. Callers do not know which backend they got; that is the point of
the ABC.

Selection is driven purely by ``settings.BLOB_STORAGE_BACKEND``:

* ``"local"``  → :class:`~aeam.storage.blob_store.LocalDiskBlobStore`
                 (default, byte-identical to the pre-E4 behavior; the
                 dev/test posture and single-node deployments)
* ``"s3"``     → :class:`~aeam.storage.s3_blob_store.S3BlobStore`
                 (any S3-compatible endpoint; the production posture
                 for ephemeral-compute deployments like Cloud Run)

All S3 credential and endpoint values are resolved through
:class:`~aeam.integrations.secret_manager.SecretManager` — the E3
landing zone. This module never reads ``os.environ`` directly and never
writes credential values to logs.

Rollback: flip ``BLOB_STORAGE_BACKEND`` back to ``"local"`` and the
LocalDiskBlobStore is reinstated for the next startup. No data
migration is destructive because content-addressing makes copy-forward
between backends safe.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.storage.blob_store import BlobStore, LocalDiskBlobStore

logger = logging.getLogger(__name__)


def build_blob_store(settings: Any, secret_manager: Any | None = None) -> BlobStore:
    """
    Construct the concrete :class:`BlobStore` implementation selected by
    ``settings.BLOB_STORAGE_BACKEND``.

    Args:
        settings:        Application :class:`~aeam.config.settings.Settings`
                         instance.
        secret_manager:  Optional :class:`~aeam.integrations.secret_manager.SecretManager`.
                         When provided, S3 credentials/endpoint are
                         resolved through it (env-first, settings-fallback).
                         When ``None`` (the default), values on ``settings``
                         are used directly — the correct behavior for local
                         and test posture, where no secret manager is
                         needed.

    Returns:
        A :class:`BlobStore` instance. The caller must not depend on the
        concrete class (that is the ABC's contract).

    Raises:
        ValueError:   If the selected backend is not recognised or a
                      required S3 setting is missing.
        RuntimeError: If ``BLOB_STORAGE_BACKEND=s3`` is selected but
                      ``boto3`` is not installed (raised by
                      :class:`S3BlobStore` on construction).
    """
    backend = (getattr(settings, "BLOB_STORAGE_BACKEND", "local") or "local").strip().lower()

    if backend == "local":
        root = getattr(settings, "BLOB_STORAGE_DIR", "data/blobs")
        logger.info("BlobStore backend selected: local | root=%s", root)
        return LocalDiskBlobStore(root)

    if backend == "s3":
        # Import lazily so importing this factory never requires boto3
        # when the local backend is selected (the common dev-time path).
        from aeam.storage.s3_blob_store import S3BlobStore

        def _resolve(key: str, default: Any = None) -> Any:
            """Prefer SecretManager (env-first) → fall back to settings attr."""
            if secret_manager is not None:
                v = secret_manager.get_secret(key, default=None)
                if v is not None and str(v).strip() != "":
                    return v
            v = getattr(settings, key, None)
            return v if v not in (None, "") else default

        bucket = _resolve("BLOB_S3_BUCKET")
        if not bucket:
            raise ValueError(
                "BLOB_STORAGE_BACKEND=s3 requires BLOB_S3_BUCKET to be set "
                "(via env, Settings, or SecretManager). Startup aborted "
                "(Phase E4, ARCH-7)."
            )

        logger.info(
            "BlobStore backend selected: s3 | bucket=%s | endpoint=%s",
            bucket,
            _resolve("BLOB_S3_ENDPOINT_URL") or "<aws-default>",
        )
        return S3BlobStore(
            bucket=str(bucket),
            endpoint_url=_resolve("BLOB_S3_ENDPOINT_URL") or None,
            region_name=_resolve("BLOB_S3_REGION") or None,
            access_key_id=_resolve("BLOB_S3_ACCESS_KEY_ID") or None,
            secret_access_key=_resolve("BLOB_S3_SECRET_ACCESS_KEY") or None,
            prefix=str(_resolve("BLOB_S3_PREFIX", "") or ""),
        )

    raise ValueError(
        f"Unknown BLOB_STORAGE_BACKEND={backend!r}. Must be one of: 'local', 's3'."
    )
