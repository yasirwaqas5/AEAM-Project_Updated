"""
aeam/storage/blob_store.py

Content-addressable blob storage for the Enterprise Data Layer (Phase B1.1).

Stores original uploaded files keyed by the SHA-256 hash of their bytes, so
identical content is stored exactly once (idempotent) and every stored object
is verifiable by its address. The registry ``versions.blob_ref`` column holds
the URI returned here.

This module defines an abstract :class:`BlobStore` plus a
:class:`LocalDiskBlobStore` implementation. An S3 or Azure Blob backend can be
added later by implementing the same abstract interface — callers depend only
on :class:`BlobStore`, never on a concrete backend, so no caller changes when
the backend is swapped.

No upload endpoints, no ingestion, no parsing live here — this is storage only.
"""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

# Read files in 1 MiB chunks when hashing/streaming from disk.
_CHUNK = 1024 * 1024


class BlobNotFoundError(KeyError):
    """Raised when a requested content hash is not present in the store."""


def compute_content_hash(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data`` — the canonical blob address."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class BlobRef:
    """
    A reference to a stored blob.

    Attributes:
        content_hash: SHA-256 hex digest — the object's content address.
        size:         Size in bytes.
        uri:          Backend-qualified locator stored in ``versions.blob_ref``
                      (e.g. ``local://<hash>``, later ``s3://bucket/<hash>``).
    """

    content_hash: str
    size: int
    uri: str


class BlobStore(ABC):
    """
    Abstract content-addressable blob store.

    Contract:
    - :meth:`put` is idempotent — storing identical bytes twice yields the same
      address and does not duplicate data.
    - Objects are addressed solely by their SHA-256 content hash.
    - Implementations must never mutate stored content (write-once).

    Subclass this for S3 / Azure without touching any caller.
    """

    #: URI scheme prefix for this backend (e.g. ``"local"``, ``"s3"``).
    scheme: str = "blob"

    @abstractmethod
    def put(self, data: bytes, *, content_type: str | None = None) -> BlobRef:
        """
        Store ``data`` and return its :class:`BlobRef`.

        Idempotent: if the content already exists, returns the existing ref
        without rewriting. ``content_type`` is accepted for future backends
        (e.g. S3 metadata) and may be ignored by content-only backends.
        """

    @abstractmethod
    def get(self, content_hash: str) -> bytes:
        """Return the bytes for ``content_hash``. Raises :class:`BlobNotFoundError` if absent."""

    @abstractmethod
    def exists(self, content_hash: str) -> bool:
        """Return ``True`` if ``content_hash`` is present."""

    @abstractmethod
    def delete(self, content_hash: str) -> bool:
        """Delete ``content_hash``. Returns ``True`` if it existed, ``False`` otherwise."""

    @abstractmethod
    def stat(self, content_hash: str) -> BlobRef | None:
        """Return the :class:`BlobRef` for ``content_hash``, or ``None`` if absent."""

    def uri_for(self, content_hash: str) -> str:
        """Return the backend-qualified URI for a content hash."""
        return f"{self.scheme}://{content_hash}"


class LocalDiskBlobStore(BlobStore):
    """
    Local-filesystem content-addressable blob store.

    Objects are written to ``<root>/<h0h1>/<h2h3>/<hash>`` — a two-level fan-out
    that keeps any single directory small. Writes are atomic (temp file + rename)
    so a crashed write never leaves a partial, mis-addressed object.

    Args:
        root_dir: Directory to store blobs under. Created if it does not exist.

    Raises:
        ValueError: If ``root_dir`` is empty.
        OSError:    If the directory cannot be created.
    """

    scheme = "local"

    def __init__(self, root_dir: str | os.PathLike[str]) -> None:
        if not str(root_dir).strip():
            raise ValueError("root_dir must be a non-empty path.")
        self._root = Path(root_dir).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        logger.info("LocalDiskBlobStore initialised | root=%s", self._root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, data: bytes, *, content_type: str | None = None) -> BlobRef:
        if data is None:
            raise ValueError("data must not be None.")
        content_hash = compute_content_hash(data)
        path = self._path_for(content_hash)

        if path.exists():
            # Idempotent: identical content already stored.
            return BlobRef(content_hash, path.stat().st_size, self.uri_for(content_hash))

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic within the same filesystem
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        logger.debug("blob stored | hash=%s | size=%d", content_hash, len(data))
        return BlobRef(content_hash, len(data), self.uri_for(content_hash))

    def get(self, content_hash: str) -> bytes:
        path = self._path_for(content_hash)
        if not path.exists():
            raise BlobNotFoundError(content_hash)
        return path.read_bytes()

    def open(self, content_hash: str) -> BinaryIO:
        """Return a binary read stream for large blobs (local-disk convenience)."""
        path = self._path_for(content_hash)
        if not path.exists():
            raise BlobNotFoundError(content_hash)
        return open(path, "rb")

    def exists(self, content_hash: str) -> bool:
        return self._path_for(content_hash).exists()

    def delete(self, content_hash: str) -> bool:
        path = self._path_for(content_hash)
        if not path.exists():
            return False
        path.unlink()
        return True

    def stat(self, content_hash: str) -> BlobRef | None:
        path = self._path_for(content_hash)
        if not path.exists():
            return None
        return BlobRef(content_hash, path.stat().st_size, self.uri_for(content_hash))

    @property
    def root(self) -> Path:
        """The root directory of this store."""
        return self._root

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path_for(self, content_hash: str) -> Path:
        h = str(content_hash).strip().lower()
        if len(h) < 4 or not all(c in "0123456789abcdef" for c in h):
            raise ValueError(f"invalid content_hash: {content_hash!r}")
        return self._root / h[:2] / h[2:4] / h

    def __repr__(self) -> str:
        return f"LocalDiskBlobStore(root={self._root!s})"
