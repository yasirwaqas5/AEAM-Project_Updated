"""
aeam/registry

Enterprise asset registries for the Data Layer (Phase B1.1 — Storage Foundation).

Exposes the domain models (Document, Dataset, Source, Schema, Version,
IngestionJob) and their repositories. Persistence-only: no ingestion,
classification, or lifecycle orchestration lives here — those arrive in later
B1 phases and compose these primitives.
"""

from aeam.registry.models import (
    Source,
    Document,
    Dataset,
    Schema,
    Version,
    IngestionJob,
    SourceKind,
    SourceStatus,
    AssetStatus,
    JobType,
    JobStatus,
    ParentType,
)
from aeam.registry.repositories import (
    SourceRepository,
    DocumentRepository,
    DatasetRepository,
    SchemaRepository,
    VersionRepository,
    IngestionJobRepository,
)

__all__ = [
    # models
    "Source", "Document", "Dataset", "Schema", "Version", "IngestionJob",
    # vocabularies
    "SourceKind", "SourceStatus", "AssetStatus", "JobType", "JobStatus", "ParentType",
    # repositories
    "SourceRepository", "DocumentRepository", "DatasetRepository",
    "SchemaRepository", "VersionRepository", "IngestionJobRepository",
]
