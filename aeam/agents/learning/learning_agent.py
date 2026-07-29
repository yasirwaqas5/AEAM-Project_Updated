"""
aeam/agents/learning/learning_agent.py

The Learning Agent (Phase F2 — Adaptive Learning, Feedback Loop &
Confidence Recalibration).

This agent owns the feedback loop that E9 and E12 made possible but that
nothing consumed: human verdicts and resolved-incident outcomes were
recorded and then never used to improve anything. It reads them as labeled
signal, asks :class:`~aeam.intelligence.calibration.CalibrationEngine` for a
mapping, and — when that mapping measurably improves held-out calibration —
persists it as a new **version** of the platform's calibration state.

What makes it an agent rather than a script is what it is *not allowed* to
do. Two boundaries are structural, not conventional:

**1. It never mutates history (MEM-2).** Every read is a ``SELECT``. The
agent has no code path that updates an incident, a verdict, or a finding,
and ``test_learning_run_mutates_no_historical_row`` proves it by hashing
every row of every historical table before and after a run. Learning from
the record must never mean editing it.

**2. It never changes a threshold (AGENT-5).** It may *propose* that an
automation threshold move, with the measurement that motivated the
proposal attached. The proposal sits ``pending`` until a human records a
verdict through the privileged review surface. There is deliberately no
method on this class that applies a proposal — an advisory agent that can
enact its own advice is not advisory, and the absence of the method is the
enforcement.

Calibration itself is the one thing the agent *does* change, and it is
bounded three ways: it is flag-gated (flag-off yields raw confidence
exactly), it is versioned (any prior calibration can be restored), and it
is refused unless it measurably improves on data the fit never saw.

Composition only. The statistics live in ``calibration.py``; persistence is
the injected database client; the E9 verdicts and incident outcomes are
read through the tables those phases already own. Nothing here reimplements
any of them.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aeam.intelligence.calibration import (
    CalibrationEngine,
    CalibrationFit,
    apply_calibration,
    extract_labeled_samples,
)

logger = logging.getLogger(__name__)

# Engine-owned defaults (ENG-6).
_DEFAULT_HISTORY_LIMIT: int = 5000

#: Proposal states. Mirrors the E9 verdict vocabulary deliberately: the two
#: governance surfaces should be readable by the same operator without
#: learning a second set of words (COMPAT-6 — vocabularies only grow).
PROPOSAL_PENDING: str = "pending"
PROPOSAL_APPROVED: str = "approved"
PROPOSAL_REJECTED: str = "rejected"
VALID_VERDICTS: frozenset[str] = frozenset({PROPOSAL_APPROVED, PROPOSAL_REJECTED})


class LearningError(Exception):
    """Base class for Learning Agent failures."""


class ProposalNotFoundError(LearningError):
    """The referenced proposal does not exist."""


class ProposalConflictError(LearningError):
    """The proposal has already been decided; verdicts are not re-castable."""


class InvalidVerdictError(LearningError):
    """The verdict is not one this surface accepts."""


class LearningAgent:
    """Owns the feedback loop, calibration state, and threshold proposals.

    Args:
        database_client: The platform's :class:`DatabaseClient`. Required —
                         an agent whose entire job is reading persisted
                         outcomes cannot function without one, and
                         defaulting it to None would make a silent no-op
                         look like a working feedback loop.
        engine:          Calibration engine override (tests, tuning).
                         ``None`` builds one with the module defaults.
        history_limit:   Maximum incidents read per recalibration run.

    Raises:
        ValueError: If ``database_client`` is None or ``history_limit`` < 2.
    """

    def __init__(
        self,
        database_client: Any,
        engine: CalibrationEngine | None = None,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> None:
        if database_client is None:
            raise ValueError("database_client must not be None.")
        if history_limit < 2:
            raise ValueError("history_limit must be >= 2.")
        self._db = database_client
        self._engine = engine or CalibrationEngine()
        self._history_limit = int(history_limit)

    # ------------------------------------------------------------------
    # Reading the feedback signal (SELECT only — MEM-2)
    # ------------------------------------------------------------------

    def collect_signal(self) -> tuple[list[Any], dict[str, int]]:
        """Read labeled outcomes from history.

        Both queries are reads. The incident's ``investigation_status`` is
        not a stored column — it lives inside the ``audit_summary`` findings
        entry — so it is parsed out here rather than recomputed, which would
        risk deriving a status different from the one the incident was
        finalized with (EXPL-2: explanations restate, never recompute).

        Returns:
            ``(samples, skipped)`` from
            :func:`~aeam.intelligence.calibration.extract_labeled_samples`.
        """
        rows = self._db.fetch_all(
            "SELECT incident_id, confidence, findings FROM incidents "
            "ORDER BY timestamp DESC LIMIT :limit",
            params={"limit": self._history_limit},
        )

        incidents: list[dict[str, Any]] = []
        for row in rows:
            incidents.append({
                "incident_id": row.get("incident_id"),
                "confidence": row.get("confidence"),
                "investigation_status": _status_from_findings(row.get("findings")),
            })

        verdicts: list[dict[str, Any]] = []
        try:
            verdicts = self._db.fetch_all(
                "SELECT incident_id, verdict, created_at FROM review_verdicts "
                "ORDER BY created_at ASC"
            )
        except Exception as exc:  # noqa: BLE001
            # A deployment that never enabled E9 has no verdicts table. That
            # is a normal posture, not an error: the loop falls back to
            # status-derived outcomes and says how many samples it got.
            logger.info("LearningAgent | no verdict signal available (%s)", exc)

        return extract_labeled_samples(incidents, verdicts)

    # ------------------------------------------------------------------
    # Recalibration
    # ------------------------------------------------------------------

    def recalibrate(self, created_by: str = "system", dry_run: bool = False) -> dict[str, Any]:
        """Fit a calibration from history and adopt it if it measurably helps.

        Args:
            created_by: Acting principal, recorded on the persisted version.
            dry_run:    Measure and report without persisting. The operator's
                        "what would this do?" — a governance surface that
                        can only be exercised by committing to its result is
                        one nobody will exercise.

        Returns:
            A dict describing the run: the fit's own measurements plus
            ``adopted``, ``version`` and ``calibration_id`` when persisted.
            Never raises on a refusal — a calibration that does not improve
            is a normal, reportable outcome.
        """
        samples, skipped = self.collect_signal()
        fit = self._engine.fit(samples, skipped=skipped)

        result: dict[str, Any] = {
            "adopted": False,
            "dry_run": dry_run,
            "version": None,
            "calibration_id": None,
            "labeled_samples": len(samples),
            **fit.to_dict(),
        }

        if not fit.usable:
            logger.info(
                "LearningAgent.recalibrate | not adopted | samples=%d | %s",
                len(samples), fit.reason,
            )
            return result

        if dry_run:
            result["reason"] = (
                "Dry run: the calibration measurably improved held-out ECE "
                f"({fit.before.ece:.6f} -> {fit.after.ece:.6f}) but was not persisted."
            )
            return result

        calibration_id, version = self._persist_calibration(fit, created_by)
        result.update({"adopted": True, "version": version, "calibration_id": calibration_id})

        logger.info(
            "LearningAgent.recalibrate | ADOPTED v%d | id=%s | ece %.6f -> %.6f | by=%s",
            version, calibration_id, fit.before.ece, fit.after.ece, created_by,
        )
        return result

    def _persist_calibration(self, fit: CalibrationFit, created_by: str) -> tuple[str, int]:
        """Write a new active calibration version, superseding the previous.

        Superseding marks the old row rather than deleting it: the mapping
        that produced a historical incident's calibrated confidence must
        stay inspectable, and it is the mechanism the rollback strategy
        depends on (COMPAT-7).
        """
        now = datetime.now(tz=timezone.utc).isoformat()
        current = self.active_calibration()
        version = int(current["version"]) + 1 if current else 1

        if current:
            self._db.execute(
                "UPDATE calibration_models SET status = 'superseded', superseded_at = :now "
                "WHERE calibration_id = :cid",
                params={"now": now, "cid": current["calibration_id"]},
            )

        calibration_id = str(uuid.uuid4())
        self._db.insert(
            table="calibration_models",
            data={
                "calibration_id": calibration_id,
                "version": version,
                "status": "active",
                "knots": json.dumps([list(k) for k in fit.knots]),
                "training_samples": fit.training_samples,
                "holdout_samples": fit.holdout_samples,
                "ece_before": fit.before.ece if fit.before else None,
                "ece_after": fit.after.ece if fit.after else None,
                "brier_before": fit.before.brier if fit.before else None,
                "brier_after": fit.after.brier if fit.after else None,
                "curve_before": json.dumps(fit.before.to_dict() if fit.before else None),
                "curve_after": json.dumps(fit.after.to_dict() if fit.after else None),
                "skipped_counts": json.dumps(fit.skipped),
                "source_window": f"most recent {self._history_limit} incidents",
                "created_by": created_by,
                "reason": (
                    f"Held-out ECE improved {fit.before.ece:.6f} -> {fit.after.ece:.6f} "
                    f"on {fit.holdout_samples} samples the fit never saw."
                ),
                "created_at": now,
            },
            returning_column="calibration_id",
        )
        return calibration_id, version

    def active_calibration(self) -> dict[str, Any] | None:
        """Return the live calibration row, or ``None`` when none is active.

        Never raises: a deployment that has never recalibrated, or whose
        table does not exist yet, has no active calibration — which is a
        state, not a failure. Callers apply raw confidence in that case.
        """
        try:
            rows = self._db.fetch_all(
                "SELECT * FROM calibration_models WHERE status = 'active' "
                "ORDER BY version DESC LIMIT 1"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LearningAgent | calibration state unavailable (%s)", exc)
            return None
        return rows[0] if rows else None

    def calibration_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return calibration versions, newest first — the rollback ledger."""
        try:
            return self._db.fetch_all(
                "SELECT calibration_id, version, status, training_samples, holdout_samples, "
                "ece_before, ece_after, brier_before, brier_after, created_by, reason, "
                "created_at, superseded_at FROM calibration_models "
                "ORDER BY version DESC LIMIT :limit",
                params={"limit": int(limit)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LearningAgent | calibration history unavailable (%s)", exc)
            return []

    def restore_calibration(self, version: int, restored_by: str) -> dict[str, Any]:
        """Make a previous calibration version active again.

        The named rollback path. It creates no new mapping and re-measures
        nothing — it re-points ``active`` at a version whose measurements
        are already recorded, which is exactly what makes rollback safe.

        Raises:
            LearningError: If the version does not exist.
        """
        rows = self._db.fetch_all(
            "SELECT calibration_id, version FROM calibration_models WHERE version = :v",
            params={"v": int(version)},
        )
        if not rows:
            raise LearningError(f"No calibration version {version} exists.")

        now = datetime.now(tz=timezone.utc).isoformat()
        current = self.active_calibration()
        if current and int(current["version"]) != int(version):
            self._db.execute(
                "UPDATE calibration_models SET status = 'superseded', superseded_at = :now "
                "WHERE calibration_id = :cid",
                params={"now": now, "cid": current["calibration_id"]},
            )

        self._db.execute(
            "UPDATE calibration_models SET status = 'active', superseded_at = NULL "
            "WHERE calibration_id = :cid",
            params={"cid": rows[0]["calibration_id"]},
        )

        logger.warning(
            "LearningAgent.restore_calibration | v%d restored by %s", version, restored_by,
        )
        return {"restored_version": int(version), "restored_by": restored_by, "restored_at": now}

    # ------------------------------------------------------------------
    # Advisory proposals (AGENT-5)
    # ------------------------------------------------------------------

    def propose_threshold(
        self,
        subject: str,
        current_value: Any,
        proposed_value: Any,
        rationale: str,
        evidence: dict[str, Any] | None = None,
        proposal_type: str = "automation_threshold",
    ) -> dict[str, Any]:
        """Record a threshold change proposal for human decision.

        The proposal takes effect on nothing. There is no counterpart method
        that applies it, and that absence is the AGENT-5 boundary: the agent
        can put a recommendation in front of a human and can do nothing at
        all if the human never answers.

        Raises:
            ValueError: If ``subject`` or ``rationale`` is empty. A proposal
                        an operator cannot evaluate is worse than none —
                        it invites rubber-stamping.
        """
        if not subject or not subject.strip():
            raise ValueError("subject must be a non-empty string.")
        if not rationale or not rationale.strip():
            raise ValueError(
                "rationale must be a non-empty string — an unexplained proposal "
                "cannot be meaningfully approved."
            )

        proposal_id = str(uuid.uuid4())
        self._db.insert(
            table="learning_proposals",
            data={
                "proposal_id": proposal_id,
                "proposal_type": proposal_type,
                "subject": subject.strip(),
                "current_value": str(current_value),
                "proposed_value": str(proposed_value),
                "rationale": rationale.strip(),
                "evidence": json.dumps(evidence or {}),
                "status": PROPOSAL_PENDING,
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            returning_column="proposal_id",
        )

        logger.info(
            "LearningAgent.propose_threshold | proposal=%s | subject=%s | %s -> %s (PENDING)",
            proposal_id, subject, current_value, proposed_value,
        )
        return {"proposal_id": proposal_id, "status": PROPOSAL_PENDING, "subject": subject}

    def list_proposals(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """List proposals, newest first, optionally filtered by status."""
        try:
            if status:
                return self._db.fetch_all(
                    "SELECT * FROM learning_proposals WHERE status = :s "
                    "ORDER BY created_at DESC LIMIT :limit",
                    params={"s": status, "limit": int(limit)},
                )
            return self._db.fetch_all(
                "SELECT * FROM learning_proposals ORDER BY created_at DESC LIMIT :limit",
                params={"limit": int(limit)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LearningAgent | proposals unavailable (%s)", exc)
            return []

    def decide_proposal(
        self,
        proposal_id: str,
        verdict: str,
        reviewer_id: str,
        reviewer_roles: list[str] | None = None,
        attribution_source: str = "unattributed",
        note: str = "",
    ) -> dict[str, Any]:
        """Record a human verdict on a proposal.

        A decided proposal is terminal. Re-deciding is refused rather than
        overwritten, so the audit trail shows one decision per proposal and
        a later reviewer cannot quietly reverse an earlier one — the same
        contract E9 verdicts hold.

        Raises:
            InvalidVerdictError:  Verdict outside the accepted vocabulary.
            ProposalNotFoundError: No such proposal.
            ProposalConflictError: Already decided.
            ValueError:            No reviewer identity supplied.
        """
        verdict = (verdict or "").strip().lower()
        if verdict not in VALID_VERDICTS:
            raise InvalidVerdictError(
                f"verdict must be one of {sorted(VALID_VERDICTS)}. Got: {verdict!r}."
            )
        if not reviewer_id or not str(reviewer_id).strip():
            raise ValueError(
                "reviewer_id must be a non-empty string — an unattributed "
                "governance decision is not a governance decision."
            )

        rows = self._db.fetch_all(
            "SELECT proposal_id, status, subject, proposed_value FROM learning_proposals "
            "WHERE proposal_id = :pid",
            params={"pid": proposal_id},
        )
        if not rows:
            raise ProposalNotFoundError(f"No proposal {proposal_id!r}.")

        existing = rows[0]
        if str(existing.get("status")) != PROPOSAL_PENDING:
            raise ProposalConflictError(
                f"Proposal {proposal_id!r} is already {existing.get('status')!r}; "
                "verdicts are recorded once and never overwritten."
            )

        now = datetime.now(tz=timezone.utc).isoformat()
        self._db.execute(
            "UPDATE learning_proposals SET status = :status, reviewer_id = :rid, "
            "reviewer_roles = :roles, attribution_source = :attr, note = :note, "
            "decided_at = :now WHERE proposal_id = :pid",
            params={
                "status": verdict,
                "rid": str(reviewer_id).strip(),
                "roles": json.dumps(reviewer_roles or []),
                "attr": attribution_source,
                "note": note or "",
                "now": now,
                "pid": proposal_id,
            },
        )

        logger.warning(
            "LearningAgent.decide_proposal | proposal=%s | verdict=%s | reviewer=%s | subject=%s",
            proposal_id, verdict, reviewer_id, existing.get("subject"),
        )
        return {
            "proposal_id": proposal_id,
            "status": verdict,
            "reviewer_id": str(reviewer_id).strip(),
            "decided_at": now,
            "subject": existing.get("subject"),
            "proposed_value": existing.get("proposed_value"),
            # Stated explicitly in the response because it is the single
            # most important property of this surface: approving a proposal
            # records agreement, it does not move the threshold.
            "applied": False,
            "note": (
                "Approved proposals are NOT auto-applied. Update the setting "
                "through the deployment configuration; this record is the "
                "authorization for that change."
            ) if verdict == PROPOSAL_APPROVED else None,
        }

    def __repr__(self) -> str:
        return f"LearningAgent(history_limit={self._history_limit}, engine={self._engine!r})"


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _status_from_findings(findings: Any) -> str | None:
    """Read ``investigation_status`` out of an incident's audit_summary.

    The status is not a column; E1 consolidated it into the audit_summary
    findings entry. Reading it there — rather than re-deriving it from
    root_cause/requires_human — guarantees the label matches the status the
    incident was actually finalized with.

    Returns ``None`` for any shape it cannot read, which
    :func:`extract_labeled_samples` counts as "no outcome signal" rather
    than guessing.
    """
    if not findings:
        return None
    try:
        entries = json.loads(findings) if isinstance(findings, str) else findings
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "audit_summary":
            status = entry.get("investigation_status")
            return str(status) if status else None
    return None


def calibrate_confidence(
    confidence: float | None,
    calibration: dict[str, Any] | None,
) -> tuple[float | None, dict[str, Any]]:
    """Apply an active calibration to one confidence value.

    The single place raw confidence becomes calibrated confidence, used by
    the Orchestrator at finalize. Returns the disclosure alongside the value
    because EXPL-4 requires an adjustment to be reported with its magnitude
    and its reason — a silently-shifted number is worse than an uncalibrated
    one, since nobody can tell it moved.

    Args:
        confidence:  The raw confidence, or ``None``.
        calibration: An active ``calibration_models`` row, or ``None``.

    Returns:
        ``(calibrated_or_raw, disclosure)``. ``disclosure`` always carries
        ``applied`` and, when it is False, the ``reason`` — the three-state
        honesty contract (EXPL-3) applied to calibration.
    """
    if confidence is None:
        return None, {"applied": False, "reason": "No confidence was produced for this incident."}

    if not calibration:
        return confidence, {
            "applied": False,
            "reason": "No active calibration; raw confidence is reported unchanged.",
            "confidence_raw": confidence,
        }

    raw_knots = calibration.get("knots")
    try:
        knots_list = json.loads(raw_knots) if isinstance(raw_knots, str) else raw_knots
        knots = [(float(x), float(y)) for x, y in (knots_list or [])]
    except (TypeError, ValueError) as exc:
        logger.warning("calibrate_confidence | unreadable knots: %s", exc)
        return confidence, {
            "applied": False,
            "reason": f"Active calibration v{calibration.get('version')} has unreadable knots.",
            "confidence_raw": confidence,
        }

    if not knots:
        return confidence, {
            "applied": False,
            "reason": f"Active calibration v{calibration.get('version')} carries no mapping.",
            "confidence_raw": confidence,
        }

    calibrated = apply_calibration(float(confidence), knots)
    return calibrated, {
        "applied": True,
        "confidence_raw": round(float(confidence), 4),
        "confidence_calibrated": calibrated,
        "adjustment": round(calibrated - float(confidence), 4),
        "calibration_version": calibration.get("version"),
        "calibration_id": calibration.get("calibration_id"),
        "ece_before": calibration.get("ece_before"),
        "ece_after": calibration.get("ece_after"),
        "reason": (
            f"Calibration v{calibration.get('version')}, fitted on "
            f"{calibration.get('training_samples')} labeled outcomes and validated on "
            f"{calibration.get('holdout_samples')} held out."
        ),
    }
