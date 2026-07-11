"""
aeam/storage

Storage primitives for the Enterprise Data Layer (Phase B1.1).

Currently exposes the content-addressable :class:`~aeam.storage.blob_store.BlobStore`
abstraction and its local-disk implementation. Designed so an S3 / Azure Blob
implementation can be added later without changing any caller.
"""

from aeam.storage.blob_store import (
    BlobStore,
    BlobRef,
    LocalDiskBlobStore,
    BlobNotFoundError,
    compute_content_hash,
)

__all__ = [
    "BlobStore",
    "BlobRef",
    "LocalDiskBlobStore",
    "BlobNotFoundError",
    "compute_content_hash",
]
