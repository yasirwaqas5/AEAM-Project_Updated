"""
aeam/registry/models.py

Shared domain models for enterprise assets (Phase B1.1 — Storage Foundation).

Plain ``@dataclass`` objects — one per registry table — that repositories map
rows to and from. They carry NO business workflow: only field definitions,
sensible defaults, a ``to_row()`` (insert-ready dict) and a ``from_row()``
(DB-dict → model, decoding JSON columns). Designed for extension: every model
has an ``extra`` JSON escape hatch so later phases can attach fields without a
schema migration, and status/kind vocabularies are centralised below.

No ORM, no framework — these compose with the existing DatabaseClient.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Controlled vocabularies (string constants — DB/JSON friendly, no enum coupling)
# ---------------------------------------------------------------------------

class SourceKind:
    UPLOAD = "upload"
    CONFLUENCE = "confluence"
    SHAREPOINT = "sharepoint"
    S3 = "s3"
    AZURE_BLOB = "azure_blob"
    DATABASE = "database"
    REST = "rest"
    GSHEET = "gsheet"
    ALL = {UPLOAD, CONFLUENCE, SHAREPOINT, S3, AZURE_BLOB, DATABASE, REST, GSHEET}


class SourceStatus:
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"
    ALL = {ACTIVE, ERROR, DISABLED}


class AssetStatus:
    """Lifecycle shared by documents and datasets."""
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    STALE = "stale"
    ARCHIVED = "archived"
    DELETED = "deleted"
    ERROR = "error"
    ALL = {PENDING, PROCESSING, INDEXED, STALE, ARCHIVED, DELETED, ERROR}


class PolicyStatus:
    """
    Lifecycle of an extracted policy (Phase E12, COMPAT-6).

    Deliberately SEPARATE from :class:`AssetStatus`: a policy's lifecycle is
    a governance decision (is this rule still in force?), not an ingestion
    lifecycle (has this file been indexed?). Conflating them would make
    "retired" and "archived" mean the same thing in one vocabulary and
    different things in another.

    ``ACTIVE`` is the default for every row, including every row that
    predates this phase, so policy matching is unchanged until an operator
    deliberately transitions something.
    """
    ACTIVE = "active"
    #: Flagged for human review — still matches, but visibly queued for a
    #: decision. Matching is unchanged so a review backlog never silently
    #: degrades investigation quality.
    PENDING_REVIEW = "pending_review"
    #: Withdrawn from force. NEVER matches a new investigation. The row is
    #: retained (not deleted) so historical incidents that cited it stay
    #: explainable.
    RETIRED = "retired"
    ALL = {ACTIVE, PENDING_REVIEW, RETIRED}
    #: Statuses a policy must hold to participate in matching.
    MATCHABLE = {ACTIVE, PENDING_REVIEW}


class CompiledRuleStatus:
    """
    Lifecycle of a compiled rule proposal (Phase F3, AGENT-5 / SEC-7).

    Deliberately separate from :class:`PolicyStatus`: a policy's lifecycle
    answers "is this document-derived knowledge still trustworthy?"; a
    compiled rule's lifecycle answers "has a human authorized this
    THRESHOLD to actually gate detection?" — a strictly narrower, more
    consequential question, which is why it needs its own, more guarded
    vocabulary rather than reusing the policy one.

    ``PROPOSED`` is the ONLY state a newly compiled rule can start in — an
    override is never enforced (never included in
    :class:`~aeam.agents.kpi.rule_engine.RuleEngine`'s ``overrides``) until
    it is ``APPROVED``. ``REJECTED`` and ``RETIRED`` are both terminal-ish:
    a rejected proposal is dead (propose again to try a different value); a
    retired rule was once approved and is being deliberately withdrawn —
    the named F3 rollback path.
    """
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETIRED = "retired"
    ALL = {PROPOSED, APPROVED, REJECTED, RETIRED}
    #: The only status whose override is loaded into RuleEngine at startup.
    ADOPTED = {APPROVED}


class SemanticDocType:
    """
    DECLARED semantic document type (Phase E12, MOD-4 / RAG-7).

    Distinct from ``Document.doc_type``, which holds the FORMAT category the
    upload validator detected ("markdown", "pdf", "docx"). Storing a format
    where retrieval expects a semantic type is the contract defect this
    phase fixes: an uploaded runbook could never earn
    :class:`~aeam.agents.rag.advanced_retrieval.BusinessRelevanceScorer`'s
    authoritative-source bonus, because its ``doc_type`` said "markdown".

    The values below are the ones the scorer's
    ``DEFAULT_ACTIONABLE_DOC_TYPES`` allowlist already recognises, plus the
    non-actionable ones an operator legitimately needs to declare. Declaring
    a type is always OPTIONAL — an undeclared document falls back to its
    format exactly as before (COMPAT-1).
    """
    RUNBOOK = "runbook"
    SRE_RUNBOOK = "sre_runbook"
    INCIDENT_REPORT = "incident_report"
    POST_MORTEM = "post_mortem"
    POLICY = "policy"
    WIKI = "wiki"
    API_DOC = "api_doc"
    REFERENCE = "reference"
    ALL = {
        RUNBOOK, SRE_RUNBOOK, INCIDENT_REPORT, POST_MORTEM,
        POLICY, WIKI, API_DOC, REFERENCE,
    }


class JobType:
    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"
    SYNC = "sync"
    ALL = {INGEST, REINDEX, DELETE, SYNC}


class JobStatus:
    QUEUED = "queued"
    VALIDATING = "validating"
    EXTRACTING = "extracting"
    INDEXING = "indexing"
    DONE = "done"
    FAILED = "failed"
    # Phase B1.2: distinct terminal state for an operator-cancelled job — never
    # was an error, so must not be conflated with FAILED. Purely additive: no
    # existing value renamed or removed, no schema change (status is TEXT).
    CANCELLED = "cancelled"
    ALL = {QUEUED, VALIDATING, EXTRACTING, INDEXING, DONE, FAILED, CANCELLED}
    #: Statuses from which a job can no longer transition.
    TERMINAL = {DONE, FAILED, CANCELLED}


class ParentType:
    DOCUMENT = "document"
    DATASET = "dataset"
    SOURCE_SYNC = "source_sync"
    ALL = {DOCUMENT, DATASET, SOURCE_SYNC}


class ApprovalStatus:
    """
    Lifecycle of ONE incident's approval requirement (Phase E9).

    ``PENDING`` covers both "no tier has approved yet" and "some, but not
    all, tiers of an ordered chain have approved" — ``IncidentApproval
    .current_tier`` says which. Gated steps execute exactly once, on the
    transition into ``APPROVED``; ``REJECTED`` halts the chain permanently
    and no gated step ever runs.
    """
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ALL = {PENDING, APPROVED, REJECTED}
    #: Statuses from which the approval can no longer transition.
    TERMINAL = {APPROVED, REJECTED}


class Verdict:
    """
    The four review verdicts (Phase E9) — the SAME vocabulary the Human
    Review workspace has offered since it shipped, now persisted.

    Only ``APPROVED`` advances the approval chain, and only ``REJECTED``
    halts it. ``CHANGES_REQUESTED`` and ``ESCALATED`` are genuine recorded
    verdicts with reviewer attribution that leave the incident pending —
    they express "not yet / not by me", which is neither an approval nor a
    rejection, and pretending otherwise would misstate what the human did.
    """
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    ESCALATED = "escalated"
    ALL = {APPROVED, REJECTED, CHANGES_REQUESTED, ESCALATED}
    #: Verdicts that move the approval chain to its next tier.
    ADVANCING = {APPROVED}
    #: Verdicts that terminate the chain without executing anything.
    HALTING = {REJECTED}


class AttributionSource:
    """
    Where a verdict's acting principal came from (Phase E9, PHIL-1).

    Recorded per verdict so the audit trail never implies an identity was
    cryptographically established when it was not.
    """
    #: Principal taken from a verified JWT (the E3 identity path).
    JWT = "jwt"
    #: Principal supplied in the request body — only reachable when the
    #: security middleware is bypassed (ENVIRONMENT=development).
    REQUEST = "request"
    #: No identity available at all.
    UNATTRIBUTED = "unattributed"
    ALL = {JWT, REQUEST, UNATTRIBUTED}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC now as an ISO-8601 string (matches how DatabaseClient serialises datetimes)."""
    return datetime.now(tz=timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _decode_json(value: Any, default: Any) -> Any:
    """
    Normalise a JSON column read from the DB.

    PostgreSQL JSONB returns dict/list already; SQLite returns a JSON string.
    Returns ``default`` for NULL/blank/unparseable values so callers never crash.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return default
    return default


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass
class _Asset:
    """Common serialisation behaviour for every registry model."""

    def to_row(self) -> dict[str, Any]:
        """
        Insert-ready column→value mapping.

        Dict/list values (JSON columns) are passed through — the existing
        DatabaseClient.insert() serialises them to JSON. ``extra`` is always
        emitted as a JSON object.
        """
        row = asdict(self)
        return row

    @staticmethod
    def _base_from_row(cls, row: dict[str, Any], json_fields: tuple[str, ...]) -> Any:
        """Shared row→model construction that decodes the named JSON columns."""
        data = dict(row)
        for f in json_fields:
            if f in data:
                data[f] = _decode_json(data[f], {} if f in ("config", "columns", "relationships", "metric_columns", "extra") else [])
        # Drop any DB columns the dataclass doesn't declare (forward-compat).
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class Source(_Asset):
    name: str = ""
    kind: str = SourceKind.UPLOAD
    source_id: str = field(default_factory=_new_id)
    config: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    status: str = SourceStatus.ACTIVE
    sync_schedule: str | None = None
    last_synced_at: str | None = None
    created_at: str = field(default_factory=_now_iso)
    created_by: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Source":
        return _Asset._base_from_row(cls, row, ("config",))


@dataclass
class Document(_Asset):
    title: str = ""
    doc_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    origin_path: str | None = None
    #: FORMAT category detected at upload ("markdown", "pdf", …) — NOT a
    #: semantic type. See ``semantic_type`` below and :class:`SemanticDocType`.
    doc_type: str | None = None
    #: Phase E12 (MOD-4/RAG-7): the DECLARED semantic type, one of
    #: :data:`SemanticDocType.ALL`. ``None`` until declared, in which case
    #: retrieval falls back to ``doc_type`` exactly as it did pre-E12.
    semantic_type: str | None = None
    current_version: int = 1
    content_hash: str | None = None
    chunk_count: int = 0
    status: str = AssetStatus.PENDING
    review_by: str | None = None
    language: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Document":
        return _Asset._base_from_row(cls, row, ())


@dataclass
class Dataset(_Asset):
    name: str = ""
    dataset_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    schema_id: str | None = None
    row_count: int = 0
    metric_columns: list[str] = field(default_factory=list)
    refresh_schedule: str | None = None
    status: str = AssetStatus.PENDING
    last_ingested_at: str | None = None
    created_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Dataset":
        return _Asset._base_from_row(cls, row, ("metric_columns",))


@dataclass
class Schema(_Asset):
    object_name: str = ""
    schema_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    columns: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    discovered_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Schema":
        return _Asset._base_from_row(cls, row, ("columns", "relationships"))


@dataclass
class Version(_Asset):
    parent_type: str = ParentType.DOCUMENT
    parent_id: str = ""
    version: int = 1
    version_id: str = field(default_factory=_new_id)
    content_hash: str | None = None
    blob_ref: str | None = None
    chunk_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    created_by: str | None = None
    is_active: bool = True

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Version":
        return _Asset._base_from_row(cls, row, ("chunk_ids",))


@dataclass
class IngestionJob(_Asset):
    job_type: str = JobType.INGEST
    job_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    status: str = JobStatus.QUEUED
    progress: int = 0
    stage: str | None = None
    error: str | None = None
    content_hash: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "IngestionJob":
        return _Asset._base_from_row(cls, row, ())


@dataclass
class Policy(_Asset):
    """
    A single structured business rule extracted from a document (Phase C2).

    Additional, not a replacement — the source document/chunk are always
    retained so a policy is traceable back to exactly what it was derived
    from. Every field besides ``policy_id``/``doc_id``/``raw_text`` is
    optional: extraction never fabricates a value for a field the source
    text didn't actually specify (see aeam/intelligence/policy_extraction.py).
    """
    policy_id: str = field(default_factory=_new_id)
    doc_id: str = ""                         # -> documents.doc_id (unenforced)
    source_document: str | None = None       # human-readable title/origin_path
    source_chunk: str | None = None          # chunk_id within the document, if attributable
    raw_text: str = ""                       # verbatim source sentence(s) this policy is based on
    business_rule: str | None = None         # short human-readable summary
    condition: str | None = None
    threshold: str | None = None
    actions: list[str] = field(default_factory=list)
    escalation_rule: str | None = None
    approval_required: bool | None = None
    department: str | None = None
    role: str | None = None
    time_constraint: str | None = None
    priority: str | None = None
    related_metrics: list[str] = field(default_factory=list)
    extracted_at: str = field(default_factory=_now_iso)
    # Phase E6: stored embedding of the policy's text, computed ONCE at
    # extraction time so PolicyRegistry no longer re-embeds the whole corpus
    # per incident. ``None`` until computed (pre-E6 rows, or extraction
    # without an embedding service) — PolicyRegistry falls back to on-the-fly
    # embedding for those, so behaviour is unchanged until backfilled.
    embedding: list[float] | None = None
    embedding_model: str | None = None
    # Phase E12 (COMPAT-6): governance lifecycle. Defaults to ACTIVE, which
    # is precisely the behaviour every pre-E12 row already had, so adopting
    # the lifecycle changes no investigation's policy matching until an
    # operator deliberately transitions a policy.
    status: str = PolicyStatus.ACTIVE
    status_changed_at: str | None = None
    status_changed_by: str | None = None
    status_reason: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Policy":
        policy = _Asset._base_from_row(cls, row, ("actions", "related_metrics"))
        # Phase E12: a row written before this phase (or by a path that never
        # set the column) reads back as ACTIVE — the status it effectively
        # had. Never None: PolicyRegistry must never have to guess whether an
        # unstatused policy is in force.
        if not policy.status:
            policy.status = PolicyStatus.ACTIVE
        # Decode embedding with a None default so "never computed" (NULL)
        # stays distinguishable from an (invalid) empty vector. A stored
        # vector is a JSON list; NULL/blank stays None.
        raw = row.get("embedding")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            policy.embedding = None
        elif isinstance(raw, list):
            policy.embedding = raw
        elif isinstance(raw, str):
            try:
                decoded = json.loads(raw)
                policy.embedding = decoded if isinstance(decoded, list) else None
            except (ValueError, TypeError):
                policy.embedding = None
        return policy


@dataclass
class CompiledRule(_Asset):
    """
    A candidate RuleEngine override compiled from one policy (Phase F3).

    Always begins ``PROPOSED``. Nothing about its existence changes any
    investigation's deterministic decision path — that only happens once a
    human approves it (SEC-7, AGENT-5) AND the platform restarts, so the
    approved override can be merged into
    :class:`~aeam.agents.kpi.rule_engine.RuleEngine`'s config at
    construction time (the same "restart-applied configuration" trade-off
    Phase D4's Enterprise Configuration Engine already documents — this
    reuses that posture rather than introducing a new one).

    Retained forever, like :class:`Policy` and :class:`IncidentApproval`:
    a rejected or retired row stays queryable so "why was this threshold
    ever proposed, and who decided against it" remains answerable.
    """
    rule_id: str = field(default_factory=_new_id)
    policy_id: str = ""                # -> policies.policy_id (unenforced)
    domain: str = ""                   # RuleEngine domain, e.g. "sales"
    rule_key: str = ""                 # e.g. "daily_drop_percent"
    comparison: str | None = None      # documentation only; RuleEngine's own evaluator acts
    value: float | None = None
    rationale: str | None = None       # the compiler's own explanation
    status: str = CompiledRuleStatus.PROPOSED
    created_at: str = field(default_factory=_now_iso)
    created_by: str | None = None
    reviewer_id: str | None = None
    reviewer_roles: list[str] = field(default_factory=list)
    attribution_source: str | None = None
    note: str | None = None
    decided_at: str | None = None
    retired_at: str | None = None
    retired_by: str | None = None
    retired_reason: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "CompiledRule":
        rule = _Asset._base_from_row(cls, row, ("reviewer_roles",))
        if not rule.status:
            rule.status = CompiledRuleStatus.PROPOSED
        return rule


@dataclass
class IncidentApproval(_Asset):
    """
    ONE incident's approval requirement and the gated steps it holds back
    (Phase E9 — Human-in-the-Loop Enforcement).

    Created by the Orchestrator at finalization ONLY when the execution
    plan set ``human_approval_required=True`` and enforcement is on. Its
    existence is what makes the gate real: ``pending_actions`` is the
    exact, ordered list of ActionAgent calls that were NOT executed, each
    recorded with the parameters the Orchestrator had already built, so an
    approval later executes precisely what was withheld — never a
    re-derived or re-planned set.

    MEM-2: this is a NEW record about an incident, never a mutation of the
    incident row. An incident that predates this phase simply has no
    approval record, and every reader treats absence as "no gate".
    """
    approval_id: str = field(default_factory=_new_id)
    #: ``incidents.incident_id`` of the persisted incident (unenforced FK).
    incident_id: str = ""
    #: The Orchestrator's own per-investigation id (its IncidentContext id),
    #: which is NOT the same value as ``incident_id`` — the incidents table
    #: generates its own primary key on insert. Kept for traceability
    #: between the findings JSON and this record.
    investigation_id: str | None = None
    event_type: str | None = None
    metric: str | None = None
    severity: str | None = None
    status: str = ApprovalStatus.PENDING
    #: Ordered approval chain, e.g. ``["reviewer"]`` (single-tier default)
    #: or ``["analyst", "manager", "risk"]``.
    required_tiers: list[str] = field(default_factory=list)
    #: Index into ``required_tiers`` of the tier whose approval is awaited.
    #: Equals ``len(required_tiers)`` once every tier has approved.
    current_tier: int = 0
    #: ``[{"step": "diagnostics", "params": {...}}, ...]`` — exactly what
    #: was withheld, in runbook order.
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    #: Runbook step names that actually returned SUCCESS on approval.
    executed_actions: list[str] = field(default_factory=list)
    #: ``[{"action": ..., "reason": ...}]`` for steps that did not run.
    skipped_actions: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "IncidentApproval":
        return _Asset._base_from_row(
            cls, row,
            ("required_tiers", "pending_actions", "executed_actions", "skipped_actions"),
        )


@dataclass
class ReviewVerdict(_Asset):
    """
    ONE human verdict, attributed to ONE principal, at ONE tier of an
    approval chain (Phase E9).

    Verdicts are append-only: a chain of three tiers produces three rows,
    each with its own reviewer, so "who approved what, and in which order"
    is answerable from the table alone. A rejection is recorded the same
    way — the row names the tier and principal that halted the chain.
    """
    verdict_id: str = field(default_factory=_new_id)
    #: -> incident_approvals.approval_id (unenforced FK).
    approval_id: str = ""
    #: -> incidents.incident_id (unenforced FK); denormalised so verdict
    #: history is queryable per incident without a join.
    incident_id: str = ""
    #: 0-based index into the approval's ``required_tiers`` at the time
    #: this verdict was cast.
    tier: int = 0
    tier_label: str | None = None
    verdict: str = Verdict.APPROVED
    reviewer_id: str = ""
    reviewer_roles: list[str] = field(default_factory=list)
    #: See :class:`AttributionSource` — how the principal was established.
    attribution_source: str = AttributionSource.UNATTRIBUTED
    note: str | None = None
    created_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ReviewVerdict":
        return _Asset._base_from_row(cls, row, ("reviewer_roles",))
