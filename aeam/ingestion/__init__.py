"""
aeam/ingestion

Enterprise ingestion infrastructure (Phases B1.2 + B1.3).

Composes the B1.1 Storage Foundation (BlobStore + registry) into an upload
entry point (B1.2), a background job worker (B1.2), and the real processing
pipeline (B1.3): per-format text extraction that feeds the existing RAG
:class:`~aeam.agents.rag.ingestion_pipeline.IngestionPipeline` (chunk → embed →
index into Qdrant) and finalises the Document/Version registry rows.

The worker executes a pluggable :data:`JobProcessor`; B1.3 supplies the real
:class:`~aeam.ingestion.processor.DocumentIngestJobProcessor` (Tier 1+2 formats:
PDF, DOCX, Markdown, CSV, Excel, JSON, XML, Log/Text). The B1.2
``PlaceholderJobProcessor`` remains available for infrastructure-only tests.
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
from aeam.ingestion.extraction import (
    ExtractionError,
    ExtractionResult,
    UnsupportedCategoryError,
    can_extract,
    extract_text,
    PROCESSABLE_CATEGORIES,
    DEFERRED_CATEGORIES,
)
from aeam.ingestion.processor import DocumentIngestJobProcessor, ProcessingError

__all__ = [
    "IngestValidationError",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_MIME_TYPES",
    "MAX_UPLOAD_BYTES",
    "validate_upload",
    "IngestionWorker",
    "JobProcessor",
    "PlaceholderJobProcessor",
    # B1.3 — extraction + real processor
    "ExtractionError",
    "ExtractionResult",
    "UnsupportedCategoryError",
    "can_extract",
    "extract_text",
    "PROCESSABLE_CATEGORIES",
    "DEFERRED_CATEGORIES",
    "DocumentIngestJobProcessor",
    "ProcessingError",
]
