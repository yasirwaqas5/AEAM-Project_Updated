"""
aeam/ingestion

Enterprise ingress infrastructure (Phase B1.2 — Ingress API + Async Job System).

Composes the B1.1 Storage Foundation (BlobStore + registry) into an upload
entry point and a background job worker. Deliberately contains NO parsing,
chunking, embedding, OCR, or Qdrant indexing — the worker executes a
pluggable :data:`JobProcessor` callable, and Phase B1.2 supplies only a
placeholder that proves the async infrastructure without doing real work.
Real per-format extraction lands in a later phase by swapping that callable.
"""

from aeam.ingestion.validation import (
    IngestValidationError,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_MIME_TYPES,
    MAX_UPLOAD_BYTES,
    validate_upload,
)
from aeam.ingestion.worker import (
    IngestionWorker,
    JobProcessor,
    PlaceholderJobProcessor,
)

__all__ = [
    "IngestValidationError",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_MIME_TYPES",
    "MAX_UPLOAD_BYTES",
    "validate_upload",
    "IngestionWorker",
    "JobProcessor",
    "PlaceholderJobProcessor",
]
