"""
aeam/ingestion

Enterprise ingestion infrastructure (Phases B1.2 + B1.3 + B1.4).

Composes the B1.1 Storage Foundation (BlobStore + registry) into an upload
entry point (B1.2), a background job worker (B1.2), and the real processing
pipelines:

- B1.3 documents: per-format text extraction that feeds the existing RAG
  :class:`~aeam.agents.rag.ingestion_pipeline.IngestionPipeline` (chunk → embed
  → index into Qdrant) and finalises the Document/Version registry rows.
- B1.4 datasets: structured (CSV/Excel) files are profiled — schema inference
  (column types/roles + metric columns) — and registered as Schema + Dataset +
  Version rows.

The worker executes a pluggable :data:`JobProcessor`. A
:class:`~aeam.ingestion.routing.RoutingJobProcessor` dispatches each job by
parent type to the :class:`~aeam.ingestion.processor.DocumentIngestJobProcessor`
(B1.3) or the :class:`~aeam.ingestion.dataset_processor.DatasetIngestJobProcessor`
(B1.4). The B1.2 ``PlaceholderJobProcessor`` remains for infrastructure tests.
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
from aeam.ingestion.schema_inference import (
    SchemaInferenceError,
    infer_schema,
    infer_dataset_schema,
    read_primary_table,
)
from aeam.ingestion.dataset_processor import DatasetIngestJobProcessor, DatasetProcessingError
from aeam.ingestion.routing import RoutingJobProcessor

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
    # B1.4 — schema inference + dataset processor + routing
    "SchemaInferenceError",
    "infer_schema",
    "infer_dataset_schema",
    "read_primary_table",
    "DatasetIngestJobProcessor",
    "DatasetProcessingError",
    "RoutingJobProcessor",
]
