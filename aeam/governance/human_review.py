"""
aeam/governance/human_review.py

Human-in-the-Loop enforcement (Phase E9).

Since Phase C7 the ExecutionPlanningEngine has computed
``human_approval_required`` for every incident — and nothing enforced it. The
safe-action runbook executed regardless, so the platform's only real
protection was the reversible-action catalog itself, and the Human Review
workspace could offer verdicts that went nowhere. AGENT-5 permits an
unenforced flag only if it is documented as advisory; this module is what
makes it enforced instead.

Three responsibilities, and deliberately no more:

1. **Resolve the approval chain** for an incident — the ORDERED list of
   tiers that must each approve before gated steps run
   (:func:`resolve_approval_chain`). A single-tier chain is the default and
   behaves exactly like a one-step approval (COMPAT-1/6).
2. **Record what was withheld** at finalization
   (:meth:`HumanReviewService.record_pending_approval`) — the exact
   ActionAgent calls the Orchestrator built but did not execute.
3. **Apply a human verdict** (:meth:`HumanReviewService.submit_verdict`) —
   attribute it to a principal at a tier, advance or halt the chain, and,
   when the LAST tier approves, execute precisely the recorded steps through
   the **unchanged** :class:`~aeam.agents.action.action_agent.ActionAgent`.

What this module is NOT: it does not plan, re-plan, or re-derive actions; it
never authenticates or authorises a caller (that is the E3 middleware and
RBAC, applied at the router); and it never invents an approval. An incident
with no approval record has no gate — which is exactly how every incident
predating this phase reads back (COMPAT-1).

Honesty contract: a verdict row always records how the acting principal was
established (:class:`~aeam.registry.models.AttributionSource`), and an
execution result reports only what ActionAgent actually confirmed — the same
"never claim an action ran unless it did" rule the Orchestrator already
follows.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.agents.orchestrator.runbooks import resolve_action_step
from aeam.monitoring.metrics import (
    action_failure_total,
    action_success_total,
    agent_execution_time,
    end_timer,
    start_timer,
)
from aeam.registry.models import (
    ApprovalStatus,
    AttributionSource,
    IncidentApproval,
    ReviewVerdict,
    Verdict,
    _now_iso,
)
from aeam.registry.repositories import (
    IncidentApprovalRepository,
    ReviewVerdictRepository,
)

logger = logging.getLogger(__name__)

#: Fallback chain used when configuration supplies nothing usable. One tier —
#: i.e. today's single-step approval — never zero, because a chain of zero
#: tiers would mean "gated but instantly satisfiable", which is not a gate.
_FALLBACK_CHAIN: tuple[str, ...] = ("reviewer",)


class HumanReviewError(Exception):
    """Base class for review-workflow failures (never used for auth)."""


class ApprovalNotFoundError(HumanReviewError):
    """No approval record exists for the incident — there is nothing to gate."""


class ApprovalConflictError(HumanReviewError):
    """
    The verdict cannot be applied to the chain in its current state — e.g.
    approving a chain a previous tier already rejected.
    """


class InvalidVerdictError(HumanReviewError):
    """The supplied verdict is not one of :class:`~aeam.registry.models.Verdict`."""


# ---------------------------------------------------------------------------
# Approval-chain resolution
# ---------------------------------------------------------------------------

def parse_tier_chain(raw: str | None) -> list[str]:
    """
    Parse a comma-separated tier chain (same convention as
    ``ACTIVATED_DATASET_IDS``).

    Order is significant and preserved; blanks are dropped; duplicates are
    removed (keeping first position) because a chain cannot meaningfully
    require the same tier twice.

    Args:
        raw: e.g. ``"analyst,manager,risk"``. ``None``/blank yields ``[]``.

    Returns:
        Ordered, de-duplicated tier labels.
    """
    if not raw:
        return []
    seen: set[str] = set()
    chain: list[str] = []
    for part in raw.split(","):
        label = part.strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        chain.append(label)
    return chain


def parse_chain_overrides(raw: str | None) -> dict[str, list[str]]:
    """
    Parse per-severity chain overrides formatted
    ``"CRITICAL:analyst,manager,risk;HIGH:analyst,manager"``.

    Severity keys are upper-cased so lookup is case-insensitive. A malformed
    segment is skipped with a warning rather than raising — a typo in one
    override must not prevent the platform from starting, and the default
    chain (which is stricter than no chain) still applies.

    Args:
        raw: The raw setting value, or ``None``.

    Returns:
        ``{"CRITICAL": ["analyst", "manager", "risk"], ...}``.
    """
    if not raw:
        return {}
    overrides: dict[str, list[str]] = {}
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if ":" not in segment:
            logger.warning(
                "parse_chain_overrides | ignoring malformed segment %r "
                "(expected 'SEVERITY:tier1,tier2')", segment,
            )
            continue
        severity, _, chain_raw = segment.partition(":")
        chain = parse_tier_chain(chain_raw)
        if not severity.strip() or not chain:
            logger.warning(
                "parse_chain_overrides | ignoring empty override segment %r", segment,
            )
            continue
        overrides[severity.strip().upper()] = chain
    return overrides


def policy_driven_chain(findings: list[dict[str, Any]] | None) -> list[str]:
    """
    Derive an approval chain from the incident's OWN matched policies.

    A policy that both requires approval (``approval_required``) and names
    the responsible ``role`` is the organisation stating, in its own
    document, who must sign off. When several such policies matched, their
    roles form the chain in match order — the order the Policy Registry
    already ranked them in, not an order invented here.

    Policies that require approval but name no role contribute nothing to
    the chain (there is no honest way to guess who they meant); the caller
    then falls back to the configured chain, so the gate still applies.

    Args:
        findings: The incident's findings list (the ``"policy"`` entry is
                  read; everything else is ignored).

    Returns:
        Ordered, de-duplicated role labels, or ``[]`` when no matched policy
        names one.
    """
    if not findings:
        return []

    matches: list[dict[str, Any]] = []
    for entry in findings:
        if isinstance(entry, dict) and entry.get("type") == "policy":
            data = entry.get("data") or {}
            found = data.get("matches")
            if isinstance(found, list):
                matches = found

    seen: set[str] = set()
    chain: list[str] = []
    for match in matches:
        if not isinstance(match, dict) or not match.get("approval_required"):
            continue
        role = match.get("role")
        if not isinstance(role, str) or not role.strip():
            continue
        key = role.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        chain.append(role.strip())
    return chain


def resolve_approval_chain(
    severity: str | None,
    findings: list[dict[str, Any]] | None,
    settings: Any,
) -> list[str]:
    """
    Resolve the ordered approval chain this incident must clear.

    Precedence, strictest source of truth first:

    1. **Policy-driven** — roles named by the incident's own matched,
       approval-requiring policies (:func:`policy_driven_chain`). The
       organisation's written policy outranks a deployment default.
    2. **Severity override** — ``Settings.APPROVAL_TIER_CHAIN_OVERRIDES``.
    3. **Default chain** — ``Settings.APPROVAL_TIER_CHAIN`` (one tier by
       default, i.e. today's single-step approval).
    4. :data:`_FALLBACK_CHAIN` if configuration yields nothing usable.

    Args:
        severity: The incident's severity (case-insensitive), or ``None``.
        findings: The incident's findings list, for the policy tier.
        settings: The :class:`~aeam.config.settings.Settings` instance
                  (duck-typed — any object with the two fields works).

    Returns:
        A non-empty, ordered list of tier labels.
    """
    from_policy = policy_driven_chain(findings)
    if from_policy:
        return from_policy

    overrides = parse_chain_overrides(
        getattr(settings, "APPROVAL_TIER_CHAIN_OVERRIDES", None)
    )
    if severity:
        override = overrides.get(str(severity).strip().upper())
        if override:
            return override

    default_chain = parse_tier_chain(getattr(settings, "APPROVAL_TIER_CHAIN", None))
    return default_chain or list(_FALLBACK_CHAIN)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class HumanReviewService:
    """
    Records gated steps, applies human verdicts, and releases execution.

    Constructed once at startup and shared by the Orchestrator (which
    records) and the review API (which applies verdicts) — one service, one
    set of rules, no second implementation of "is this approved yet?".

    Args:
        approval_repo: Persistence for the per-incident approval record.
        verdict_repo:  Append-only persistence for verdicts.
        settings:      Application settings — read for
                       ``HUMAN_APPROVAL_ENFORCED`` and the chain
                       configuration.
        action_agent:  The SAME, unchanged
                       :class:`~aeam.agents.action.action_agent.ActionAgent`
                       the Orchestrator uses. ``None`` is legitimate (no
                       action backend wired): approvals are still recorded,
                       and every released step is honestly reported as
                       skipped rather than silently claimed as executed.

    Example::

        service = HumanReviewService(
            approval_repo=IncidentApprovalRepository(db),
            verdict_repo=ReviewVerdictRepository(db),
            settings=settings,
            action_agent=action_agent,
        )
        result = service.submit_verdict(
            incident_id="inc-1", verdict="approved",
            reviewer_id="alice", reviewer_roles=["admin"],
            attribution_source="jwt",
        )
    """

    def __init__(
        self,
        approval_repo: IncidentApprovalRepository,
        verdict_repo: ReviewVerdictRepository,
        settings: Any,
        action_agent: Any | None = None,
    ) -> None:
        if approval_repo is None or verdict_repo is None:
            raise ValueError("approval_repo and verdict_repo must not be None.")
        self._approvals = approval_repo
        self._verdicts = verdict_repo
        self._settings = settings
        self._action = action_agent

    # ---- gate posture -------------------------------------------------

    @property
    def enforced(self) -> bool:
        """
        ``True`` when the gate actually withholds execution.

        ``False`` is the documented rollback posture: the Orchestrator
        behaves exactly as it did before Phase E9 (gated steps run
        immediately) and these tables stay inert.
        """
        return bool(getattr(self._settings, "HUMAN_APPROVAL_ENFORCED", False))

    def resolve_chain(
        self, severity: str | None, findings: list[dict[str, Any]] | None,
    ) -> list[str]:
        """Chain for this incident — see :func:`resolve_approval_chain`."""
        return resolve_approval_chain(severity, findings, self._settings)

    # ---- recording ----------------------------------------------------

    def record_pending_approval(
        self,
        *,
        approval_id: str,
        incident_id: str,
        investigation_id: str | None,
        event_type: str | None,
        metric: str | None,
        severity: str | None,
        required_tiers: list[str],
        pending_actions: list[dict[str, Any]],
    ) -> IncidentApproval:
        """
        Persist the approval requirement and the steps withheld from it.

        Called by the Orchestrator at finalization, AFTER the incident row
        exists, so ``incident_id`` is the id the review queue and the
        console actually address.

        Args:
            approval_id:      Pre-generated id, so the incident's own
                              ``human_approval`` findings entry can name the
                              record it belongs to before the row is written.
            incident_id:      ``incidents.incident_id``.
            investigation_id: The Orchestrator's per-investigation id.
            event_type/metric/severity: Copied for queue rendering, so the
                              queue never needs to join to display a row.
            required_tiers:   The resolved ordered chain.
            pending_actions:  ``[{"step": ..., "params": {...}}]`` in runbook
                              order — exactly what was withheld.

        Returns:
            The persisted :class:`~aeam.registry.models.IncidentApproval`.
        """
        approval = IncidentApproval(
            approval_id=approval_id,
            incident_id=incident_id,
            investigation_id=investigation_id,
            event_type=event_type,
            metric=metric,
            severity=severity,
            status=ApprovalStatus.PENDING,
            required_tiers=list(required_tiers),
            current_tier=0,
            pending_actions=list(pending_actions),
            executed_actions=[],
            skipped_actions=[],
        )
        self._approvals.create(approval)
        logger.info(
            "HumanReviewService | approval recorded | incident_id=%s | tiers=%s | "
            "withheld=%d",
            incident_id, required_tiers, len(pending_actions),
        )
        return approval

    # ---- reading ------------------------------------------------------

    def get_approval(self, incident_id: str) -> IncidentApproval | None:
        """The approval record for an incident, or ``None`` (no gate)."""
        return self._approvals.get_by_incident(incident_id)

    def list_queue(
        self, limit: int | None = None, offset: int = 0,
    ) -> tuple[list[IncidentApproval], int]:
        """
        Pending approvals, oldest first, with the total pending count.

        Returns:
            ``(page, total)`` — ``total`` is the full pending count so a
            paged console can show queue depth without a second request
            (same X-Total-Count convention as the E6 list endpoints).
        """
        page = self._approvals.list_by_status(ApprovalStatus.PENDING, limit, offset)
        return page, self._approvals.count_by_status(ApprovalStatus.PENDING)

    def list_verdicts(
        self, limit: int | None = None, offset: int = 0,
        incident_id: str | None = None,
    ) -> tuple[list[ReviewVerdict], int]:
        """Verdict history (newest first) with its total count."""
        page = self._verdicts.list_recent(limit, offset, incident_id)
        return page, self._verdicts.count_all(incident_id)

    def verdicts_for(self, approval_id: str) -> list[ReviewVerdict]:
        """Every verdict cast against one approval chain, in chain order."""
        return self._verdicts.list_for_approval(approval_id)

    # ---- the verdict itself -------------------------------------------

    def submit_verdict(
        self,
        *,
        incident_id: str,
        verdict: str,
        reviewer_id: str,
        reviewer_roles: list[str] | None = None,
        attribution_source: str = AttributionSource.UNATTRIBUTED,
        note: str | None = None,
    ) -> dict[str, Any]:
        """
        Apply one human verdict to an incident's approval chain.

        Behaviour by verdict:

        - ``approved`` — records the verdict at the awaited tier and
          advances the chain. When that was the LAST tier, the recorded
          pending steps execute (once) through the unchanged ActionAgent
          and the approval becomes ``approved``. Otherwise the approval
          stays ``pending`` for the next tier, and nothing executes.
        - ``rejected`` — halts the chain permanently at the current tier.
          Nothing executes, ever, for this incident; each withheld step is
          recorded as skipped with the rejecting tier and principal.
        - ``changes_requested`` / ``escalated`` — recorded with full
          attribution; the chain does not advance and nothing executes.
          These say "not by me, not yet", which is neither approval nor
          rejection, and are not converted into either.

        Idempotency: a repeated approval from the same principal is a
        no-op that returns the original outcome (``idempotent: True``) —
        the pending steps can never execute twice. The same rule stops one
        principal from clearing several tiers of a chain whose entire
        purpose is to require several people.

        Args:
            incident_id:        ``incidents.incident_id``.
            verdict:            One of :class:`~aeam.registry.models.Verdict`.
            reviewer_id:        The acting principal.
            reviewer_roles:     Roles that principal held (recorded, not checked
                                — authorisation happens at the router via RBAC).
            attribution_source: See :class:`~aeam.registry.models.AttributionSource`.
            note:               Optional free-text reviewer note.

        Returns:
            A result dict: ``{"incident_id", "approval_id", "status",
            "verdict", "tier", "tier_label", "required_tiers",
            "current_tier", "remaining_tiers", "executed_actions",
            "skipped_actions", "idempotent"}``.

        Raises:
            InvalidVerdictError:    Unknown verdict string.
            ApprovalNotFoundError:  No approval record for the incident.
            ApprovalConflictError:  Verdict cannot apply to the chain's
                                    current state (e.g. approving after a
                                    rejection).
        """
        verdict = (verdict or "").strip().lower()
        if verdict not in Verdict.ALL:
            raise InvalidVerdictError(
                f"Unknown verdict {verdict!r}. Expected one of {sorted(Verdict.ALL)}."
            )

        approval = self._approvals.get_by_incident(incident_id)
        if approval is None:
            raise ApprovalNotFoundError(
                f"No approval record for incident {incident_id!r}. Either the "
                f"incident never required human approval, or it predates "
                f"Phase E9 — in both cases there is nothing to approve."
            )

        reviewer_id = (reviewer_id or "").strip() or "unattributed"
        roles = list(reviewer_roles or [])

        # --- Terminal states: never re-execute, never silently overwrite.
        if approval.status in ApprovalStatus.TERMINAL:
            return self._terminal_outcome(approval, verdict)

        # --- Idempotent repeat from the same principal.
        if verdict in Verdict.ADVANCING:
            existing = self._verdicts.find_advancing_by_reviewer(
                approval.approval_id, reviewer_id,
            )
            if existing is not None:
                logger.info(
                    "HumanReviewService | idempotent approval ignored | "
                    "incident_id=%s | reviewer=%s | original_tier=%d",
                    incident_id, reviewer_id, existing.tier,
                )
                return self._outcome(
                    approval, verdict=Verdict.APPROVED, tier=existing.tier,
                    tier_label=existing.tier_label, idempotent=True,
                )

        tier = int(approval.current_tier or 0)
        tiers = list(approval.required_tiers or [])
        tier_label = tiers[tier] if 0 <= tier < len(tiers) else None

        self._verdicts.create(ReviewVerdict(
            approval_id=approval.approval_id,
            incident_id=approval.incident_id,
            tier=tier,
            tier_label=tier_label,
            verdict=verdict,
            reviewer_id=reviewer_id,
            reviewer_roles=roles,
            attribution_source=attribution_source,
            note=note,
        ))

        # --- Rejection halts the chain; nothing ever executes.
        if verdict in Verdict.HALTING:
            reason = (
                f"Rejected at tier {tier + 1}/{len(tiers)}"
                f"{f' ({tier_label})' if tier_label else ''} by {reviewer_id}."
            )
            skipped = [
                {"action": entry.get("step"), "reason": reason}
                for entry in (approval.pending_actions or [])
            ]
            self._approvals.save_progress(
                approval.approval_id,
                status=ApprovalStatus.REJECTED,
                skipped_actions=skipped,
            )
            approval.status = ApprovalStatus.REJECTED
            approval.skipped_actions = skipped
            approval.updated_at = _now_iso()
            logger.info(
                "HumanReviewService | REJECTED | incident_id=%s | tier=%d | "
                "reviewer=%s | withheld=%d",
                incident_id, tier, reviewer_id, len(skipped),
            )
            return self._outcome(approval, verdict=verdict, tier=tier, tier_label=tier_label)

        # --- Non-advancing verdicts: recorded, chain unchanged.
        if verdict not in Verdict.ADVANCING:
            logger.info(
                "HumanReviewService | %s recorded (chain unchanged) | "
                "incident_id=%s | tier=%d | reviewer=%s",
                verdict, incident_id, tier, reviewer_id,
            )
            return self._outcome(approval, verdict=verdict, tier=tier, tier_label=tier_label)

        # --- Approval: advance, and release execution at the last tier.
        next_tier = tier + 1
        if next_tier < len(tiers):
            self._approvals.save_progress(approval.approval_id, current_tier=next_tier)
            approval.current_tier = next_tier
            approval.updated_at = _now_iso()
            logger.info(
                "HumanReviewService | tier %d/%d approved | incident_id=%s | "
                "reviewer=%s | next_tier=%s",
                tier + 1, len(tiers), incident_id, reviewer_id, tiers[next_tier],
            )
            return self._outcome(approval, verdict=verdict, tier=tier, tier_label=tier_label)

        executed, skipped = self._execute_pending(approval)
        self._approvals.save_progress(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            current_tier=next_tier,
            executed_actions=executed,
            skipped_actions=skipped,
        )
        approval.status = ApprovalStatus.APPROVED
        approval.current_tier = next_tier
        approval.executed_actions = executed
        approval.skipped_actions = skipped
        approval.updated_at = _now_iso()
        logger.info(
            "HumanReviewService | APPROVED (chain complete) | incident_id=%s | "
            "reviewer=%s | executed=%s | skipped=%s",
            incident_id, reviewer_id, executed, [s.get("action") for s in skipped],
        )
        return self._outcome(approval, verdict=verdict, tier=tier, tier_label=tier_label)

    # ---- internals ----------------------------------------------------

    def _execute_pending(
        self, approval: IncidentApproval,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """
        Execute exactly the recorded pending steps, in the recorded order.

        Mirrors the Orchestrator's own ``_run_step`` contract precisely — the
        same alias resolution, the same Prometheus counters, and the same
        rule that a step is reported executed ONLY when ActionAgent returned
        ``SUCCESS``. Nothing is re-planned: the parameters are the ones the
        Orchestrator built at finalization, including the ``incident_id`` it
        would have used, so an approved action is byte-identical to the
        action that would have run ungated.
        """
        executed: list[str] = []
        skipped: list[dict[str, Any]] = []

        if self._action is None:
            return executed, [
                {"action": entry.get("step"), "reason": "ActionAgent not available."}
                for entry in (approval.pending_actions or [])
            ]

        for entry in approval.pending_actions or []:
            step = entry.get("step")
            if not step:
                continue
            params: dict[str, Any] = dict(entry.get("params") or {})
            registry_type, extra_params = resolve_action_step(step)
            merged = {**params, **extra_params}
            action_incident_id = str(params.get("incident_id") or approval.incident_id)

            t = start_timer()
            try:
                result = self._action.execute(
                    action_type=registry_type,
                    parameters=merged,
                    incident_id=action_incident_id,
                )
            except Exception as exc:  # noqa: BLE001
                end_timer(agent_execution_time.labels(agent=f"action:{registry_type}"), t)
                action_failure_total.labels(action_type=registry_type).inc()
                skipped.append({"action": step, "reason": str(exc)})
                logger.error(
                    "HumanReviewService | approved action %s raised: %s", step, exc,
                )
                continue

            end_timer(agent_execution_time.labels(agent=f"action:{registry_type}"), t)

            if result.get("status") == "SUCCESS":
                action_success_total.labels(action_type=registry_type).inc()
                executed.append(step)
            else:
                action_failure_total.labels(action_type=registry_type).inc()
                detail: dict[str, Any] = result.get("result") or {}
                reason = (
                    result.get("failure_reason")
                    or detail.get("reason")
                    or detail.get("error")
                    or result.get("status", "unknown")
                )
                skipped.append({"action": step, "reason": str(reason)})

        return executed, skipped

    def _terminal_outcome(
        self, approval: IncidentApproval, verdict: str,
    ) -> dict[str, Any]:
        """
        Resolve a verdict that arrived after the chain already terminated.

        Repeating the verdict that produced the terminal state is
        idempotent — the caller learns nothing changed and the original
        outcome is returned. Contradicting it is a conflict, reported with
        the state that actually holds rather than silently ignored.
        """
        if approval.status == ApprovalStatus.APPROVED and verdict in Verdict.ADVANCING:
            return self._outcome(approval, verdict=verdict, idempotent=True)
        if approval.status == ApprovalStatus.REJECTED and verdict in Verdict.HALTING:
            return self._outcome(approval, verdict=verdict, idempotent=True)

        halting = next(
            (v for v in self._verdicts.list_for_approval(approval.approval_id)
             if v.verdict in Verdict.HALTING),
            None,
        )
        if approval.status == ApprovalStatus.REJECTED:
            who = halting.reviewer_id if halting else "a reviewer"
            where = (
                f"tier {halting.tier + 1} ({halting.tier_label})"
                if halting and halting.tier_label else "an earlier tier"
            )
            raise ApprovalConflictError(
                f"Incident {approval.incident_id!r} was rejected at {where} by "
                f"{who}; the approval chain is halted and no gated action can run."
            )
        raise ApprovalConflictError(
            f"Incident {approval.incident_id!r} is already approved and its "
            f"gated actions have run; it cannot now be {verdict}."
        )

    def _outcome(
        self,
        approval: IncidentApproval,
        *,
        verdict: str,
        tier: int | None = None,
        tier_label: str | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        """Build the caller-facing result dict for a submitted verdict."""
        tiers = list(approval.required_tiers or [])
        current = int(approval.current_tier or 0)
        return {
            "incident_id": approval.incident_id,
            "approval_id": approval.approval_id,
            "status": approval.status,
            "verdict": verdict,
            "tier": tier,
            "tier_label": tier_label,
            "required_tiers": tiers,
            "current_tier": current,
            "remaining_tiers": tiers[current:] if current < len(tiers) else [],
            "executed_actions": list(approval.executed_actions or []),
            "skipped_actions": list(approval.skipped_actions or []),
            "idempotent": idempotent,
        }

    def __repr__(self) -> str:
        return (
            f"HumanReviewService(enforced={self.enforced}, "
            f"action_agent={'wired' if self._action is not None else 'none'})"
        )
