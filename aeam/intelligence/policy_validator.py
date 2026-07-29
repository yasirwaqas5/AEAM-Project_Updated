"""
aeam/intelligence/policy_validator.py

The Policy Validator (Phase F3 — Policy Compilation, Validation & the
Policy Agent).

Static, deterministic consistency analysis over the policy corpus. Nothing
in C2/C3/E12 ever checked whether the policies a document-extraction pass
accumulated over time actually agree with each other — this module is that
check. It never calls an LLM (the roadmap is explicit: "static
consistency/conflict analysis"): every finding here is a pure function of
already-extracted, already-structured policy fields, so a validation run is
reproducible and its reasoning is fully inspectable.

Three conflict classes, each grounded in a concrete, cheap comparison:

* **threshold_collision** — two or more matchable policies compile (via
  :class:`~aeam.intelligence.rule_compiler.RuleCompiler`) to the SAME
  RuleEngine domain and rule key, with DIFFERENT threshold values. Only one
  value can ever be the adopted override for that key, so this is a real
  contradiction an operator must resolve before either can be safely
  adopted — which is exactly the F3 acceptance criterion this class exists
  to satisfy.
* **unreachable** — the same situation, but with an IDENTICAL value: a
  fully redundant duplicate that can never independently affect anything a
  reachable earlier policy doesn't already cover.
* **action_conflict** — two matchable policies share a related metric and
  disagree on ``approval_required`` for what reads as the same condition.
  This is deliberately narrow and deterministic (an exact field
  comparison, not sentiment/NLP over ``actions`` text) so every flagged
  pair is inspectable and never a false-positive guess.

This module makes no database writes and holds no state across calls —
every run is fresh over whatever policies the caller supplies (mirroring
:class:`~aeam.intelligence.policy_registry.PolicyRegistry`'s own
load-fresh-every-call posture, so a validation report can never go stale
between a policy edit and the next read).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aeam.intelligence.rule_compiler import CompiledRuleCandidate, RuleCompiler


@dataclass
class PolicyConflict:
    """One detected inconsistency in the policy corpus.

    Attributes:
        conflict_type: ``"threshold_collision"`` | ``"unreachable"`` |
                        ``"action_conflict"``.
        policy_ids:     Every policy involved, in the order compared.
        domain:         The RuleEngine domain, when the conflict is
                        rule-shaped (threshold_collision/unreachable).
                        ``None`` for action_conflict.
        rule_key:       The specific config key in collision, when
                        applicable.
        detail:         Human-readable explanation grounded in the actual
                        field values compared — never a generic template
                        with the specifics omitted.
    """

    conflict_type: str
    policy_ids: list[str]
    domain: str | None
    rule_key: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "policy_ids": self.policy_ids,
            "domain": self.domain,
            "rule_key": self.rule_key,
            "detail": self.detail,
        }


class PolicyValidator:
    """
    Runs static conflict analysis over a policy corpus.

    Args:
        compiler: :class:`~aeam.intelligence.rule_compiler.RuleCompiler`
                  override (tests). Defaults to a fresh instance — the
                  compiler is stateless, so sharing costs nothing and
                  constructing fresh costs nothing either.
    """

    def __init__(self, compiler: RuleCompiler | None = None) -> None:
        self._compiler = compiler or RuleCompiler()

    def validate(self, policies: list[Any]) -> list[PolicyConflict]:
        """
        Analyse ``policies`` for internal contradictions.

        Args:
            policies: Policy-like objects/dicts (duck-typed — see
                      :class:`RuleCompiler.compile`'s docstring). Callers
                      should pass only matchable (non-retired) policies —
                      a retired policy can no longer trigger anything, so a
                      "conflict" against one would flag something that is
                      not actually in force. This method does not filter by
                      status itself; the caller (the Policy Agent /
                      Knowledge Center API) already knows which set is
                      matchable and passing an unfiltered corpus is a
                      caller decision this function should not second-guess.

        Returns:
            Every detected :class:`PolicyConflict`, in a stable order
            (threshold collisions and unreachable duplicates first, grouped
            by domain/rule_key; then action conflicts). Empty when the
            corpus is internally consistent — a normal, common, honestly
            reported outcome, not a "not implemented" placeholder.
        """
        conflicts: list[PolicyConflict] = []
        conflicts.extend(self._threshold_conflicts(policies))
        conflicts.extend(self._action_conflicts(policies))
        return conflicts

    # ------------------------------------------------------------------
    # threshold_collision / unreachable
    # ------------------------------------------------------------------

    def _threshold_conflicts(self, policies: list[Any]) -> list[PolicyConflict]:
        groups: dict[tuple[str, str], list[tuple[str, float]]] = {}

        for policy in policies:
            candidate: CompiledRuleCandidate = self._compiler.compile(policy)
            if not candidate.compilable:
                continue
            key = (candidate.domain, candidate.rule_key)
            groups.setdefault(key, []).append((candidate.source_policy_id, candidate.value))

        findings: list[PolicyConflict] = []
        for (domain, rule_key), members in sorted(groups.items()):
            if len(members) < 2:
                continue

            distinct_values = {v for _, v in members}
            policy_ids = [pid for pid, _ in members]

            if len(distinct_values) == 1:
                value = next(iter(distinct_values))
                findings.append(PolicyConflict(
                    conflict_type="unreachable",
                    policy_ids=policy_ids,
                    domain=domain,
                    rule_key=rule_key,
                    detail=(
                        f"{len(members)} policies all compile to {domain}.{rule_key} = {value} "
                        "— only the first can ever be the adopted value for this key, so the "
                        "rest are fully redundant duplicates."
                    ),
                ))
            else:
                summary = ", ".join(f"{pid}={value}" for pid, value in members)
                findings.append(PolicyConflict(
                    conflict_type="threshold_collision",
                    policy_ids=policy_ids,
                    domain=domain,
                    rule_key=rule_key,
                    detail=(
                        f"{len(members)} policies compile to {domain}.{rule_key} with "
                        f"DIFFERENT values ({summary}) — only one value can ever be the "
                        "adopted override for this key; this must be resolved by an operator "
                        "before either candidate is approved."
                    ),
                ))

        return findings

    # ------------------------------------------------------------------
    # action_conflict
    # ------------------------------------------------------------------

    @staticmethod
    def _action_conflicts(policies: list[Any]) -> list[PolicyConflict]:
        """
        Flag policy pairs sharing a metric whose ``approval_required``
        disagrees.

        Deliberately the narrowest possible check: an exact boolean
        disagreement on a shared metric, never a judgement about whether
        two ``actions`` lists "really" conflict. A looser check would be a
        source of false positives an operator could not trust; this one is
        always inspectable field-by-field.
        """
        entries: list[tuple[str, str, bool]] = []
        for policy in policies:
            policy_id = _get(policy, "policy_id")
            approval = _get(policy, "approval_required")
            if approval is None:
                continue
            for metric in _get(policy, "related_metrics") or []:
                entries.append((str(metric).strip().lower(), policy_id, bool(approval)))

        by_metric: dict[str, list[tuple[str, bool]]] = {}
        for metric, policy_id, approval in entries:
            by_metric.setdefault(metric, []).append((policy_id, approval))

        findings: list[PolicyConflict] = []
        seen_pairs: set[frozenset[str]] = set()

        for metric, members in sorted(by_metric.items()):
            approvals_required = {pid for pid, approval in members if approval is True}
            approvals_not_required = {pid for pid, approval in members if approval is False}
            if not approvals_required or not approvals_not_required:
                continue

            for required_id in sorted(approvals_required):
                for not_required_id in sorted(approvals_not_required):
                    pair = frozenset({required_id, not_required_id})
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    findings.append(PolicyConflict(
                        conflict_type="action_conflict",
                        policy_ids=[required_id, not_required_id],
                        domain=None,
                        rule_key=None,
                        detail=(
                            f"Policies {required_id!r} and {not_required_id!r} both relate to "
                            f"metric {metric!r} but disagree on approval_required "
                            f"(True vs False) — contradictory escalation behaviour for the "
                            "same trigger."
                        ),
                    ))

        return findings


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
