"""
aeam/agents/policy/policy_agent.py

The Policy Agent (Phase F3 — Policy Compilation, Validation & the Policy
Agent).

Composition over the pieces C2/C3/E12/F3 already built: extraction
(:class:`~aeam.intelligence.policy_extraction.PolicyExtractor`, including
the Tier-3 tabular method), matching
(:class:`~aeam.intelligence.policy_registry.PolicyRegistry`, unmodified and
unchanged — this agent never touches matching), validation
(:class:`~aeam.intelligence.policy_validator.PolicyValidator`), and
compilation (:class:`~aeam.intelligence.rule_compiler.RuleCompiler`). This
class is the one place that turns those pure computations into persisted,
governed state.

The advisory boundary (AGENT-5), enforced structurally
--------------------------------------------------------
This agent can propose that a policy become a rule. It can record a human's
verdict on that proposal. It can withdraw a previously-approved rule. **It
has no method that makes a compiled rule take effect.** An approved rule's
override only reaches :class:`~aeam.agents.kpi.rule_engine.RuleEngine`
through the composition root (``aeam/main.py``) reading
:meth:`active_overrides` at container-construction time — the same
"restart-applied configuration" trade-off Phase D4's Enterprise
Configuration Engine already documents (MOD-6: this reuses that accepted
posture rather than introducing a new one). The absence of an "apply" or
"enact" method on this class is the enforcement, exactly as it is on
:class:`~aeam.agents.learning.learning_agent.LearningAgent`.

MEM-2 note
----------
Nothing here mutates a ``policies`` row. Every read of the policy corpus is
a ``SELECT`` through the existing, unmodified
:class:`~aeam.registry.repositories.PolicyRepository`; every write this
agent performs lands in the SEPARATE ``compiled_rules`` table. Compiling,
proposing, approving, rejecting, or retiring a rule changes nothing about
the policy it was compiled from.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.intelligence.policy_validator import PolicyConflict, PolicyValidator
from aeam.intelligence.rule_compiler import CompiledRuleCandidate, RuleCompiler
from aeam.registry.models import CompiledRuleStatus, PolicyStatus

logger = logging.getLogger(__name__)


class PolicyAgentError(Exception):
    """Base class for Policy Agent failures."""


class PolicyNotFoundError(PolicyAgentError):
    """The referenced policy does not exist."""


class RuleNotFoundError(PolicyAgentError):
    """The referenced compiled rule does not exist."""


class NotCompilableError(PolicyAgentError):
    """The policy could not be compiled into a RuleEngine-shaped candidate."""


class RuleConflictError(PolicyAgentError):
    """The rule is not in the lifecycle state the requested action requires."""


class PolicyAgent:
    """
    Owns policy compilation, corpus validation, and Tier-3 extraction.

    Args:
        policy_repository:       Existing
                                 :class:`~aeam.registry.repositories.PolicyRepository`
                                 — read-only from this agent's perspective.
        compiled_rule_repository: :class:`~aeam.registry.repositories.CompiledRuleRepository`
                                 — the ONLY table this agent writes to.
        compiler:                :class:`RuleCompiler` override (tests).
        validator:                :class:`PolicyValidator` override (tests).
        extractor:                Optional
                                 :class:`~aeam.intelligence.policy_extraction.PolicyExtractor`,
                                 required only for :meth:`extract_tier3`. A
                                 deployment with no LLM configured can
                                 construct this agent with ``extractor=None``
                                 and still use every other method.

    Raises:
        ValueError: If either repository is ``None``.
    """

    def __init__(
        self,
        policy_repository: Any,
        compiled_rule_repository: Any,
        compiler: RuleCompiler | None = None,
        validator: PolicyValidator | None = None,
        extractor: Any | None = None,
    ) -> None:
        if policy_repository is None:
            raise ValueError("policy_repository must not be None.")
        if compiled_rule_repository is None:
            raise ValueError("compiled_rule_repository must not be None.")
        self._policies = policy_repository
        self._rules = compiled_rule_repository
        self._compiler = compiler or RuleCompiler()
        self._validator = validator or PolicyValidator(compiler=self._compiler)
        self._extractor = extractor

    # ------------------------------------------------------------------
    # Compilation preview (no persistence)
    # ------------------------------------------------------------------

    def compile_preview(self, policy_id: str) -> CompiledRuleCandidate:
        """
        Run the compiler over ``policy_id`` without persisting anything.

        Raises:
            PolicyNotFoundError: No such policy.
        """
        policy = self._policies.get(policy_id)
        if policy is None:
            raise PolicyNotFoundError(f"No policy with id {policy_id!r}.")
        return self._compiler.compile(policy)

    # ------------------------------------------------------------------
    # Proposal (write — but PROPOSED is never enforced)
    # ------------------------------------------------------------------

    def propose_rule(self, policy_id: str, created_by: str) -> dict[str, Any]:
        """
        Compile ``policy_id`` and persist the result as a PROPOSED rule.

        Proposing changes nothing about detection behaviour — a proposed
        rule is not in :meth:`active_overrides` and never will be until a
        human approves it (SEC-7).

        Raises:
            PolicyNotFoundError: No such policy.
            RuleConflictError:   The policy is RETIRED — proposing a rule
                                 from knowledge nobody trusts any more would
                                 undermine the E12 lifecycle that retired it.
            NotCompilableError:  The compiler could not produce a candidate;
                                 the error message carries the compiler's
                                 own reason.
        """
        policy = self._policies.get(policy_id)
        if policy is None:
            raise PolicyNotFoundError(f"No policy with id {policy_id!r}.")

        status = getattr(policy, "status", PolicyStatus.ACTIVE) or PolicyStatus.ACTIVE
        if status == PolicyStatus.RETIRED:
            raise RuleConflictError(
                f"Policy {policy_id!r} is retired; a rule cannot be proposed from "
                "knowledge that is no longer trusted. Reactivate the policy first "
                "if this was retired in error."
            )

        candidate = self._compiler.compile(policy)
        if not candidate.compilable:
            raise NotCompilableError(candidate.reason)

        rule_id = self._rules.create_from_candidate(candidate, created_by=created_by)
        logger.info(
            "PolicyAgent.propose_rule | rule=%s | policy=%s | %s.%s = %s | by=%s",
            rule_id, policy_id, candidate.domain, candidate.rule_key, candidate.value, created_by,
        )
        return {
            "rule_id": rule_id,
            "policy_id": policy_id,
            "domain": candidate.domain,
            "rule_key": candidate.rule_key,
            "value": candidate.value,
            "status": CompiledRuleStatus.PROPOSED,
        }

    # ------------------------------------------------------------------
    # Human decision (AGENT-5 / SEC-7)
    # ------------------------------------------------------------------

    def decide_rule(
        self,
        rule_id: str,
        verdict: str,
        reviewer_id: str,
        reviewer_roles: list[str] | None = None,
        attribution_source: str = "unattributed",
        note: str = "",
    ) -> dict[str, Any]:
        """
        Record a human verdict on a proposed rule.

        Approval does not, by itself, change any running process's
        behaviour — see the module docstring. The response says so
        explicitly (``"effective": "next restart"``), matching F2's
        ``"applied": false`` honesty pattern.

        Raises:
            RuleNotFoundError:  No such rule.
            RuleConflictError:  The rule is not currently PROPOSED — a
                                decided rule's verdict is never overwritten.
            ValueError:         ``verdict`` outside {"approved","rejected"},
                                or no reviewer identity supplied.
        """
        verdict = (verdict or "").strip().lower()
        if verdict not in (CompiledRuleStatus.APPROVED, CompiledRuleStatus.REJECTED):
            raise ValueError(
                f"verdict must be 'approved' or 'rejected'. Got: {verdict!r}."
            )
        if not reviewer_id or not str(reviewer_id).strip():
            raise ValueError(
                "reviewer_id must be a non-empty string — an unattributed "
                "governance decision is not a governance decision."
            )

        rule = self._rules.get(rule_id)
        if rule is None:
            raise RuleNotFoundError(f"No compiled rule {rule_id!r}.")
        if getattr(rule, "status", None) != CompiledRuleStatus.PROPOSED:
            raise RuleConflictError(
                f"Rule {rule_id!r} is already {getattr(rule, 'status', '?')!r}; "
                "verdicts are recorded once and never overwritten."
            )

        self._rules.decide(
            rule_id, verdict,
            reviewer_id=str(reviewer_id).strip(),
            reviewer_roles=reviewer_roles or [],
            attribution_source=attribution_source,
            note=note or "",
        )

        logger.warning(
            "PolicyAgent.decide_rule | rule=%s | verdict=%s | reviewer=%s | domain=%s.%s",
            rule_id, verdict, reviewer_id, getattr(rule, "domain", "?"), getattr(rule, "rule_key", "?"),
        )
        return {
            "rule_id": rule_id,
            "status": verdict,
            "reviewer_id": str(reviewer_id).strip(),
            "domain": getattr(rule, "domain", None),
            "rule_key": getattr(rule, "rule_key", None),
            "value": getattr(rule, "value", None),
            "effective": (
                "next restart — the composition root loads adopted overrides at startup"
                if verdict == CompiledRuleStatus.APPROVED
                else None
            ),
        }

    def retire_rule(self, rule_id: str, retired_by: str, reason: str | None = None) -> dict[str, Any]:
        """
        Withdraw a previously APPROVED rule — the named F3 rollback path.

        Raises:
            RuleNotFoundError: No such rule.
            RuleConflictError: The rule is not currently APPROVED — only an
                               adopted rule can be retired; a proposed or
                               rejected one was never in force to begin
                               with.
        """
        rule = self._rules.get(rule_id)
        if rule is None:
            raise RuleNotFoundError(f"No compiled rule {rule_id!r}.")
        if getattr(rule, "status", None) != CompiledRuleStatus.APPROVED:
            raise RuleConflictError(
                f"Rule {rule_id!r} is {getattr(rule, 'status', '?')!r}, not approved; "
                "only an adopted rule can be retired."
            )

        self._rules.retire(rule_id, retired_by=retired_by, reason=reason)
        logger.warning(
            "PolicyAgent.retire_rule | rule=%s | domain=%s.%s | by=%s",
            rule_id, getattr(rule, "domain", "?"), getattr(rule, "rule_key", "?"), retired_by,
        )
        return {
            "rule_id": rule_id,
            "status": CompiledRuleStatus.RETIRED,
            "effective": "next restart — the composition root re-reads adopted overrides at startup",
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_rules(self, status: str | None = None) -> list[Any]:
        """List compiled rules, optionally filtered by lifecycle status."""
        if status:
            return self._rules.list_by_status(status)
        return self._rules.list_all()

    def active_overrides(self) -> dict[str, dict[str, float]]:
        """
        Build the ``{domain: {rule_key: value}}`` override dict from every
        currently-APPROVED, non-retired compiled rule.

        This is the ONLY bridge from governed state to actual enforcement,
        and it is read-only from this agent's side — the composition root
        (``aeam/main.py``) is what passes the result into
        :class:`~aeam.agents.kpi.rule_engine.RuleEngine`'s constructor.

        When two adopted rules collide on the same ``(domain, rule_key)``
        — which :meth:`validate_corpus` would already have flagged as a
        ``threshold_collision`` before both were approved, but nothing
        prevents an operator from approving both anyway — the value from
        whichever rule was decided MOST RECENTLY wins, deterministically
        (never an arbitrary dict-iteration-order pick). A warning is
        logged naming the rule that lost.
        """
        adopted = sorted(
            self._rules.list_adopted(),
            key=lambda r: getattr(r, "decided_at", "") or "",
        )
        overrides: dict[str, dict[str, float]] = {}
        winners: dict[tuple[str, str], str] = {}

        for rule in adopted:
            domain = getattr(rule, "domain", None)
            rule_key = getattr(rule, "rule_key", None)
            value = getattr(rule, "value", None)
            if not domain or not rule_key or value is None:
                continue

            key = (domain, rule_key)
            if key in winners:
                logger.warning(
                    "PolicyAgent.active_overrides | collision at %s.%s — rule %s superseded "
                    "by more recently decided rule %s",
                    domain, rule_key, winners[key], getattr(rule, "rule_id", "?"),
                )
            winners[key] = getattr(rule, "rule_id", "?")
            overrides.setdefault(domain, {})[rule_key] = float(value)

        return overrides

    # ------------------------------------------------------------------
    # Validation (static, deterministic — no LLM)
    # ------------------------------------------------------------------

    def validate_corpus(self) -> list[PolicyConflict]:
        """
        Run :class:`PolicyValidator` over every currently matchable policy.

        Retired policies are excluded — they can no longer trigger
        anything, so a "conflict" against one would flag something that is
        not actually in force (mirrors
        :meth:`~aeam.registry.repositories.PolicyRepository.list_matchable`'s
        own contract).
        """
        policies = self._policies.list_matchable()
        return self._validator.validate(policies)

    # ------------------------------------------------------------------
    # Tier-3 extraction (delegates; requires an extractor)
    # ------------------------------------------------------------------

    def extract_tier3(
        self,
        text: str,
        chunk_ids: list[str] | None = None,
        chunk_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Recover tabular/nested-conditional policy structure via Tier-3.

        Raises:
            PolicyAgentError: No extractor was configured (no LLM boundary
                              available) — an honest, immediate failure
                              rather than a confusing downstream one.
        """
        if self._extractor is None:
            raise PolicyAgentError(
                "Tier-3 extraction requires a PolicyExtractor (LLM boundary); "
                "none was configured for this PolicyAgent."
            )
        return self._extractor.extract_tabular(text, chunk_ids=chunk_ids, chunk_metadata=chunk_metadata)

    def __repr__(self) -> str:
        return f"PolicyAgent(has_extractor={self._extractor is not None})"
