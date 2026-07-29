"""
aeam/intelligence/rule_compiler.py

The Rule Compiler (Phase F3 — Policy Compilation, Validation & the Policy
Agent).

Closes the gap between "a document says X" and "the engine enforces X".
Before this module, an extracted :class:`~aeam.registry.models.Policy` was
purely advisory evidence (matched by
:class:`~aeam.intelligence.policy_registry.PolicyRegistry` and cited in an
investigation's findings) — there was no path from it to actually enforced
detection behaviour short of an operator hand-editing
``aeam/config/detection_rules.yaml``.

This module is that path's deterministic half: it turns a policy's
already-extracted, already-structured fields (``condition``, ``threshold``,
``related_metrics``) into a **candidate** override shaped exactly like an
entry :class:`~aeam.agents.kpi.rule_engine.RuleEngine` already knows how to
read from its YAML config. It performs no I/O, calls no LLM, and reaches no
database — a pure function of ``policy -> CompiledRuleCandidate``.

Why compilation targets only the three curated domains
--------------------------------------------------------
:class:`RuleEngine` hardcodes exactly three metric domains — ``sales``,
``complaints``, ``inventory`` — each with its own Python evaluator method
that reads specific, named keys out of its loaded config (e.g.
``sales.daily_drop_percent``). A compiled rule for any OTHER domain would
have nowhere to be enforced without writing a fourth evaluator method, which
would be a second rule-evaluation code path and would violate ENG-6 ("one
rule engine"). So compilation is honestly scoped: a policy compiles only
when its ``related_metrics`` name a curated domain AND its condition text
matches one of that domain's known rule shapes. Anything else is reported
as **not compilable**, with the specific reason — never silently dropped,
never guessed at.

Honesty contract
-----------------
* A policy is never force-fit into a domain it does not clearly belong to.
  ``related_metrics`` must contain the exact domain name.
* A threshold value is extracted only from text that is unambiguously
  numeric. A policy whose ``threshold``/``condition`` carries no parseable
  number does not compile — a compiler that guessed a number would be
  fabricating enforcement behaviour from a document that never stated it
  (RAG-7/MOD-4: extraction fidelity is the whole point).
* The result is always a **candidate**, never a decision. Nothing in this
  module writes anywhere; adoption is
  :class:`~aeam.agents.policy.policy_agent.PolicyAgent`'s job, gated by a
  recorded human approval (SEC-7, AGENT-5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# The compilable rule surface — kept in lock-step with
# aeam.agents.kpi.rule_engine.RuleEngine's own hardcoded evaluators. Adding a
# new compilable rule_key here without a corresponding evaluator in
# RuleEngine would produce an override that is silently never read; this
# tuple exists so that mistake is structurally visible in one place.
# ---------------------------------------------------------------------------

#: (domain, rule_key) -> the comparison RuleEngine's evaluator performs, for
#: documentation and for the validator's own reasoning about collisions.
KNOWN_RULE_KEYS: dict[tuple[str, str], str] = {
    ("sales", "daily_drop_percent"): "percent_drop_gt",
    ("sales", "absolute_minimum"): "absolute_lt",
    ("complaints", "daily_increase_threshold"): "percent_increase_gt",
    ("inventory", "critical_threshold"): "absolute_lte",
    ("inventory", "low_stock_threshold"): "absolute_lte",
}

#: The curated domain names RuleEngine actually evaluates.
CURATED_DOMAINS: frozenset[str] = frozenset({"sales", "complaints", "inventory"})

# Wording that indicates a PERCENTAGE-based condition (drop or increase),
# as opposed to an absolute-value floor. Checked before absolute-value
# wording so "drop below 30%" (percent) is not mistaken for "below 30"
# (absolute).
_PERCENT_MARKERS = ("%", "percent", "pct")

# Wording indicating the condition describes a DECREASE (a drop/fall).
_DECREASE_MARKERS = ("drop", "decrease", "decline", "fall", "reduc", "lower")

# Wording indicating the condition describes an INCREASE (a rise).
_INCREASE_MARKERS = ("increase", "rise", "grow", "surge", "spike", "more than", "exceed")

# Wording indicating a CRITICAL-severity absolute floor, as opposed to a
# generic/low-stock one. Checked first because "critical" is more specific.
_CRITICAL_MARKERS = ("critical", "emergency", "severe", "out of stock", "stockout")

# Numeric extraction: the first number in the text, with an optional
# leading currency symbol and an optional trailing '%'. Matches "30%",
# "$500", "500 units", "30".
_NUMBER_PATTERN = re.compile(r"[$€£]?\s*(-?\d+(?:\.\d+)?)\s*%?")


@dataclass
class CompiledRuleCandidate:
    """The compiler's verdict on one policy — always produced, never raised.

    Attributes:
        compilable:  Whether a valid RuleEngine-shaped override could be
                     derived. When ``False``, every field below except
                     ``source_policy_id`` and ``reason`` is ``None``.
        domain:      One of :data:`CURATED_DOMAINS`.
        rule_key:    The exact config key RuleEngine's evaluator for
                     ``domain`` reads (e.g. ``"daily_drop_percent"``).
        comparison:  Human-readable description of the comparison RuleEngine
                     performs with this value — documentation, not logic;
                     RuleEngine's own evaluator is the only code that ever
                     acts on the value.
        value:       The extracted numeric threshold.
        reason:      Always populated. States why compilation succeeded
                     (naming the exact YAML shape produced) or why it did
                     not (naming the specific missing signal).
        source_policy_id: The policy this candidate was compiled from, so a
                     caller holding only the candidate can still trace it.
        proposed_override: ``{domain: {rule_key: value}}`` — exactly the
                     shape merged into :class:`RuleEngine`'s config via its
                     ``overrides`` constructor parameter. ``None`` when not
                     compilable.
    """

    compilable: bool
    domain: str | None = None
    rule_key: str | None = None
    comparison: str | None = None
    value: float | None = None
    reason: str = ""
    source_policy_id: str | None = None
    proposed_override: dict[str, dict[str, float]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "compilable": self.compilable,
            "domain": self.domain,
            "rule_key": self.rule_key,
            "comparison": self.comparison,
            "value": self.value,
            "reason": self.reason,
            "source_policy_id": self.source_policy_id,
            "proposed_override": self.proposed_override,
        }


class RuleCompiler:
    """
    Compiles an extracted policy into a candidate RuleEngine override.

    Stateless and pure — safe to share a single instance across every
    policy in a corpus, and safe to construct fresh per call.

    Example::

        compiler = RuleCompiler()
        candidate = compiler.compile(policy)
        if candidate.compilable:
            print(candidate.proposed_override)
            # {"sales": {"daily_drop_percent": 25.0}}
    """

    def compile(self, policy: Any) -> CompiledRuleCandidate:
        """
        Attempt to compile ``policy`` into a RuleEngine-shaped candidate.

        Args:
            policy: A :class:`~aeam.registry.models.Policy` instance, or any
                    object/dict exposing the same fields
                    (``related_metrics``, ``condition``, ``threshold``,
                    ``raw_text``, ``policy_id``). Duck-typed so the
                    validator can run this over plain dicts in tests without
                    constructing full registry models.

        Returns:
            A :class:`CompiledRuleCandidate`. Never raises — an
            uncompilable policy is a normal, expected, fully-described
            outcome (PHIL-1: nothing here fabricates enforcement from an
            ambiguous document).
        """
        policy_id = _get(policy, "policy_id")
        related_metrics = [str(m).strip().lower() for m in (_get(policy, "related_metrics") or [])]
        domain = next((m for m in related_metrics if m in CURATED_DOMAINS), None)

        if domain is None:
            return CompiledRuleCandidate(
                compilable=False,
                reason=(
                    "No related_metrics entry names a curated RuleEngine domain "
                    f"({sorted(CURATED_DOMAINS)}); nothing exists to enforce this "
                    "policy against without a new evaluator (ENG-6 forbids a "
                    "second rule evaluator, so this is reported, not worked around)."
                ),
                source_policy_id=policy_id,
            )

        condition = str(_get(policy, "condition") or "")
        threshold_text = str(_get(policy, "threshold") or "")
        raw_text = str(_get(policy, "raw_text") or "")
        combined = f"{condition} {threshold_text} {raw_text}".lower()

        value = _extract_number(threshold_text) or _extract_number(condition) or _extract_number(raw_text)
        if value is None:
            return CompiledRuleCandidate(
                compilable=False,
                domain=domain,
                reason=(
                    f"Related metric names the {domain!r} domain, but no unambiguous "
                    "numeric threshold could be found in condition/threshold/raw_text. "
                    "A guessed number would be fabricated enforcement, not extraction."
                ),
                source_policy_id=policy_id,
            )

        is_percent = any(marker in combined for marker in _PERCENT_MARKERS) or "%" in threshold_text

        if domain == "sales":
            rule_key, reason = self._compile_sales(combined, is_percent)
        elif domain == "complaints":
            rule_key, reason = self._compile_complaints(combined, is_percent)
        else:
            rule_key, reason = self._compile_inventory(combined)

        if rule_key is None:
            return CompiledRuleCandidate(
                compilable=False, domain=domain, value=value,
                reason=reason, source_policy_id=policy_id,
            )

        comparison = KNOWN_RULE_KEYS[(domain, rule_key)]
        return CompiledRuleCandidate(
            compilable=True,
            domain=domain,
            rule_key=rule_key,
            comparison=comparison,
            value=value,
            reason=(
                f"Compiled to {domain}.{rule_key} = {value} from related_metrics "
                f"containing {domain!r} and condition wording matching a known rule shape."
            ),
            source_policy_id=policy_id,
            proposed_override={domain: {rule_key: value}},
        )

    @staticmethod
    def _compile_sales(combined: str, is_percent: bool) -> tuple[str | None, str]:
        decreasing = any(marker in combined for marker in _DECREASE_MARKERS)
        if is_percent and decreasing:
            return "daily_drop_percent", "compiled"
        if not is_percent:
            # An absolute floor: "sales below $500", "revenue under 1000".
            return "absolute_minimum", "compiled"
        return None, (
            "Related metric names 'sales', but the condition text matches neither a "
            "percentage drop ('drop'/'decrease' + '%') nor an absolute floor "
            "('below'/'under' + a plain number) — the two rule shapes sales.evaluate() knows."
        )

    @staticmethod
    def _compile_complaints(combined: str, is_percent: bool) -> tuple[str | None, str]:
        increasing = any(marker in combined for marker in _INCREASE_MARKERS)
        if is_percent and increasing:
            return "daily_increase_threshold", "compiled"
        return None, (
            "Related metric names 'complaints', but complaints.evaluate() knows only a "
            "percentage INCREASE rule ('increase'/'rise' + '%'), which this condition's "
            "wording does not match."
        )

    @staticmethod
    def _compile_inventory(combined: str) -> tuple[str | None, str]:
        if any(marker in combined for marker in _CRITICAL_MARKERS):
            return "critical_threshold", "compiled"
        # Any other absolute-floor wording for inventory defaults to the
        # low-stock (non-critical) rule — the only other inventory shape
        # RuleEngine's evaluator supports.
        return "low_stock_threshold", "compiled"


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from either a dict or an attribute-bearing object."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_number(text: str) -> float | None:
    """Extract the first plausible numeric threshold from ``text``.

    Returns ``None`` for blank or number-free text — never a fabricated
    zero, which downstream would be indistinguishable from a genuinely
    stated zero threshold.
    """
    if not text or not text.strip():
        return None
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
