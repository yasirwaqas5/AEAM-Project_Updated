"""
aeam/tests/retrieval_eval.py

Retrieval evaluation harness (Phase E12, RAG-7).

**Test infrastructure, not runtime code.** Nothing in ``aeam/`` outside the
test tree imports this module; it exists so retrieval QUALITY has a
regression gate the way retrieval CORRECTNESS already does.

The audit's finding was that corpus drift is invisible: a chunking change, an
embedding-model swap, or an accidentally-deleted document can quietly degrade
what investigations retrieve, and every existing test would still pass because
they all assert on structure rather than on whether the right evidence came
back. This harness closes that: it scores a retriever against a declared
golden set (``fixtures/retrieval_golden_set.json``) and fails when quality
drops below the thresholds the fixture itself declares.

Design:

- **Retriever-agnostic.** :func:`evaluate_retrieval` takes any callable
  ``(query, k) -> list[chunk_dict]``. In gating CI that is a deterministic
  in-process retriever built from the fixture corpus, so the suite needs no
  live Qdrant, no embedding model download, and no network. Against a running
  deployment the SAME function is handed
  :meth:`~aeam.agents.rag.retrieval_debug.RetrievalDebugTracer.trace`'s final
  chunks — the tracer already replays the live pipeline exactly as an
  investigation experiences it, which is precisely why the roadmap names it
  as the instrument.
- **Honest about skips.** A case whose expected evidence is not in the corpus
  under test is SKIPPED and counted as skipped. Scoring it as a pass would
  hide a missing corpus; scoring it as a failure would blame the retriever
  for something it did not do.
- **Metrics with declared semantics.** ``recall@k`` is the fraction of a
  case's expected chunks that appear anywhere in the top-k. ``MRR`` is the
  mean reciprocal rank of the FIRST expected chunk. Both are stated here
  rather than left for a reader to infer from the arithmetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

GOLDEN_SET_PATH: Path = Path(__file__).parent / "fixtures" / "retrieval_golden_set.json"

#: A retriever is any callable taking (query, k) and returning ranked chunks.
Retriever = Callable[[str, int], list[dict[str, Any]]]


@dataclass
class CaseResult:
    """One golden-set case's outcome."""

    case_id: str
    query: str
    skipped: bool = False
    skip_reason: str | None = None
    expected: list[str] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    hits: list[str] = field(default_factory=list)
    recall_at_k: float = 0.0
    reciprocal_rank: float = 0.0
    #: Rank (1-based) of the first expected chunk, or None if none appeared.
    first_hit_rank: int | None = None


@dataclass
class EvaluationReport:
    """Aggregate result of one evaluation run, with the pass/fail verdict."""

    k: int
    min_recall_at_k: float
    min_mrr: float
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def scored(self) -> list[CaseResult]:
        """Cases that actually produced a score (skips excluded)."""
        return [c for c in self.cases if not c.skipped]

    @property
    def skipped(self) -> list[CaseResult]:
        return [c for c in self.cases if c.skipped]

    @property
    def mean_recall_at_k(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        return round(sum(c.recall_at_k for c in scored) / len(scored), 4)

    @property
    def mrr(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        return round(sum(c.reciprocal_rank for c in scored) / len(scored), 4)

    @property
    def passed(self) -> bool:
        """
        ``True`` when quality clears BOTH declared thresholds.

        A run with no scored cases never passes: "we measured nothing" is not
        evidence of quality, and returning True there would make an empty
        corpus look like a green build.
        """
        if not self.scored:
            return False
        return self.mean_recall_at_k >= self.min_recall_at_k and self.mrr >= self.min_mrr

    def failure_summary(self) -> str:
        """Human-readable reason the run failed, for a test assertion message."""
        if self.passed:
            return ""
        if not self.scored:
            return (
                "No golden-set case could be scored — every case's expected evidence was "
                "missing from the corpus under test. This is a corpus problem, not a "
                "retrieval score."
            )
        problems: list[str] = []
        if self.mean_recall_at_k < self.min_recall_at_k:
            problems.append(
                f"recall@{self.k}={self.mean_recall_at_k} < required {self.min_recall_at_k}"
            )
        if self.mrr < self.min_mrr:
            problems.append(f"MRR={self.mrr} < required {self.min_mrr}")
        misses = [c.case_id for c in self.scored if not c.hits]
        detail = f" Cases returning no expected evidence at all: {misses}." if misses else ""
        return "Retrieval quality regression: " + "; ".join(problems) + "." + detail


def load_golden_set(path: Path | None = None) -> dict[str, Any]:
    """Load the golden query set fixture."""
    return json.loads((path or GOLDEN_SET_PATH).read_text(encoding="utf-8"))


def _chunk_id(chunk: dict[str, Any]) -> str | None:
    """Extract a chunk's identifier, tolerating both retrieval shapes.

    The live pipeline puts ``chunk_id`` at the top level of a hit and also in
    its ``metadata``; a fixture retriever may only set one. Reading both means
    the harness scores the live pipeline and a test double identically.
    """
    direct = chunk.get("chunk_id")
    if isinstance(direct, str) and direct:
        return direct
    metadata = chunk.get("metadata")
    if isinstance(metadata, dict):
        nested = metadata.get("chunk_id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def evaluate_retrieval(
    retriever: Retriever,
    golden_set: dict[str, Any] | None = None,
    corpus_chunk_ids: Iterable[str] | None = None,
) -> EvaluationReport:
    """
    Score ``retriever`` against the golden set.

    Args:
        retriever:        Callable ``(query, k) -> ranked chunk dicts``.
        golden_set:       Parsed fixture; loaded from disk when omitted.
        corpus_chunk_ids: The chunk ids actually present in the corpus under
                          test. When supplied, a case whose expected evidence
                          is absent is SKIPPED rather than scored — so a
                          missing document is reported as a missing document,
                          not as a retrieval failure. When omitted, every case
                          is scored.

    Returns:
        An :class:`EvaluationReport` whose ``passed`` property is the gate.
    """
    data = golden_set if golden_set is not None else load_golden_set()
    thresholds = data.get("thresholds", {})
    k = int(thresholds.get("k", 5))
    report = EvaluationReport(
        k=k,
        min_recall_at_k=float(thresholds.get("min_recall_at_k", 0.0)),
        min_mrr=float(thresholds.get("min_mrr", 0.0)),
    )
    available = set(corpus_chunk_ids) if corpus_chunk_ids is not None else None

    for case in data.get("cases", []):
        case_id = str(case.get("id", "<unnamed>"))
        query = str(case.get("query", ""))
        expected = [str(c) for c in case.get("expected_chunk_ids", [])]

        if available is not None:
            missing = [c for c in expected if c not in available]
            if missing:
                report.cases.append(CaseResult(
                    case_id=case_id,
                    query=query,
                    skipped=True,
                    skip_reason=(
                        f"expected evidence absent from the corpus under test: {missing}"
                    ),
                    expected=expected,
                ))
                continue

        try:
            hits = retriever(query, k) or []
        except Exception as exc:  # noqa: BLE001
            # A retriever that raises is a FAILING case, never a skipped one —
            # a crash is exactly the regression this harness must catch.
            report.cases.append(CaseResult(
                case_id=case_id, query=query, expected=expected,
                retrieved=[], hits=[], recall_at_k=0.0, reciprocal_rank=0.0,
                skip_reason=f"retriever raised: {exc}",
            ))
            continue

        retrieved_ids = [cid for cid in (_chunk_id(h) for h in hits[:k]) if cid]
        found = [cid for cid in expected if cid in retrieved_ids]

        first_rank: int | None = None
        for position, cid in enumerate(retrieved_ids, start=1):
            if cid in expected:
                first_rank = position
                break

        report.cases.append(CaseResult(
            case_id=case_id,
            query=query,
            expected=expected,
            retrieved=retrieved_ids,
            hits=found,
            recall_at_k=round(len(found) / len(expected), 4) if expected else 0.0,
            reciprocal_rank=round(1.0 / first_rank, 4) if first_rank else 0.0,
            first_hit_rank=first_rank,
        ))

    return report


def tracer_retriever(tracer: Any, filter_criteria: dict[str, str] | None = None) -> Retriever:
    """
    Adapt a :class:`~aeam.agents.rag.retrieval_debug.RetrievalDebugTracer` to
    the :data:`Retriever` signature.

    This is how the harness runs against a LIVE deployment: the tracer already
    replays the real retrieval pipeline stage by stage, so evaluating through
    it measures what an investigation would actually receive rather than a
    parallel code path that could drift from it.

    Not exercised by the offline gating suite (which needs no Qdrant); it is
    the documented entry point for running the evaluation against staging, per
    docs/KNOWLEDGE_GOVERNANCE.md#retrieval-evaluation-methodology.
    """
    def _retrieve(query: str, k: int) -> list[dict[str, Any]]:
        result = tracer.trace(query=query, top_k=k, filter_criteria=filter_criteria)
        chunks = result.get("final_chunks") or result.get("chunks") or []
        return list(chunks)

    return _retrieve
