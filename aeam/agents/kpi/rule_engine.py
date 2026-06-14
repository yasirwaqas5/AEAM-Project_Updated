"""
aeam/agents/kpi/rule_engine.py

Deterministic rule-based detection for the AEAM KPI Agent.

Loads threshold configuration from ``aeam/config/detection_rules.yaml`` at
initialisation and exposes a single ``evaluate`` method that applies the
appropriate rules for a given metric domain.

This module:
- Contains no event creation logic.
- Contains no orchestrator references.
- Performs no I/O after initialisation.
- Is fully deterministic: identical inputs always produce identical outputs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# Type definitions for better static safety
# ---------------------------------------------------------------------------


class RuleOutput(TypedDict):
    """Typed dictionary for rule evaluation results."""
    rule_triggered: bool
    rule_name: str | None
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# Default rules path — resolved relative to this file's package root so the
# engine works regardless of the working directory.
# ---------------------------------------------------------------------------

_DEFAULT_RULES_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent / "config" / "detection_rules.yaml"
)


# ---------------------------------------------------------------------------
# Internal result type
# ---------------------------------------------------------------------------


class RuleResult:
    """
    Structured result produced by :meth:`RuleEngine.evaluate`.

    Attributes:
        rule_triggered: ``True`` if any rule threshold was breached.
        rule_name:      The name of the first rule that fired, or ``None``
                        if no rule was triggered.
        details:        Supplementary diagnostic data (computed values,
                        thresholds consulted, percent changes, etc.).
    """

    __slots__ = ("rule_triggered", "rule_name", "details")

    def __init__(
        self,
        rule_triggered: bool,
        rule_name: str | None,
        details: dict[str, Any],
    ) -> None:
        self.rule_triggered = rule_triggered
        self.rule_name = rule_name
        self.details = details

    def to_dict(self) -> RuleOutput:
        """
        Serialise as a plain dict matching the AEAM contract.

        Returns:
            Dict with keys ``rule_triggered``, ``rule_name``, and ``details``.
        """
        return {
            "rule_triggered": self.rule_triggered,
            "rule_name": self.rule_name,
            "details": self.details,
        }

    def __repr__(self) -> str:
        return (
            f"RuleResult(triggered={self.rule_triggered}, "
            f"rule={self.rule_name!r})"
        )


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------


class RuleEngine:
    """
    Applies deterministic threshold rules to KPI observations.

    Rules are loaded from a YAML configuration file at construction time.
    Each top-level key in the YAML corresponds to a metric domain (e.g.
    ``"sales"``, ``"complaints"``, ``"inventory"``). The ``evaluate`` method
    selects and applies the appropriate rule set for the supplied
    ``metric_name``.

    Supported metric domains and their logic:

    **sales**
        - Computes the percentage drop from ``previous`` to ``current``.
        - Fires ``sales.daily_drop_percent`` if the drop exceeds the
          configured threshold.
        - Fires ``sales.absolute_minimum`` if ``current`` falls below the
          configured floor, regardless of percentage.

    **complaints**
        - Fires ``complaints.daily_increase_threshold`` if ``current``
          exceeds ``previous * (1 + threshold / 100)``.

    **inventory**
        - Fires ``inventory.critical_threshold`` if ``current`` <=
          the critical floor.
        - Fires ``inventory.low_stock_threshold`` if ``current`` <=
          the low-stock threshold (and not already critical).

    Unknown metric domains return a non-triggered result with a diagnostic
    detail rather than raising, allowing the caller to decide how to handle
    unrecognised metrics.

    Args:
        rules_path: Path to the YAML rules file. Defaults to
                    ``aeam/config/detection_rules.yaml`` relative to the
                    package root.

    Raises:
        FileNotFoundError: If the rules file does not exist at ``rules_path``.
        ValueError:        If the YAML file is empty or cannot be parsed as a
                           mapping, or if required configuration keys are missing.
    """

    def __init__(
        self,
        rules_path: str | Path = _DEFAULT_RULES_PATH,
    ) -> None:
        """
        Load detection rules from ``rules_path``.

        Args:
            rules_path: Absolute or relative path to the YAML config file.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError:        If the YAML parses to a non-dict or is empty.
        """
        self._rules: dict[str, Any] = self._load_rules(Path(rules_path))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        metric_name: str,
        current: float,
        previous: float,
    ) -> RuleOutput:
        """
        Evaluate the appropriate rule set for ``metric_name``.

        Delegates to a domain-specific evaluation method based on
        ``metric_name``. If the domain is not recognised, returns a
        non-triggered result with a ``"reason"`` detail.

        Args:
            metric_name: The metric domain to evaluate. Case-insensitive.
                         Known values: ``"sales"``, ``"complaints"``,
                         ``"inventory"``.
            current:     The latest observed value for this metric.
            previous:    The prior period's observed value, used as the
                         baseline for percentage-change rules.

        Returns:
            A :class:`dict` with the following keys::

                {
                    "rule_triggered": bool,
                    "rule_name":      str | None,
                    "details":        dict,
                }

        Example::

            result = engine.evaluate("complaints", current=45.0, previous=20.0)
            # {"rule_triggered": True, "rule_name": "complaints.daily_increase_threshold",
            #  "details": {...}}
        """
        domain = metric_name.lower().strip()

        evaluators = {
            "sales": self._evaluate_sales,
            "complaints": self._evaluate_complaints,
            "inventory": self._evaluate_inventory,
        }

        evaluator = evaluators.get(domain)
        if evaluator is None:
            return RuleResult(
                rule_triggered=False,
                rule_name=None,
                details={
                    "reason": f"No rules configured for metric domain '{metric_name}'.",
                    "known_domains": sorted(evaluators.keys()),
                },
            ).to_dict()

        return evaluator(current=current, previous=previous).to_dict()

    @property
    def loaded_domains(self) -> list[str]:
        """
        Return a sorted list of metric domains present in the loaded rules.

        Filters out non-domain keys like 'version' that may exist in the YAML.

        Returns:
            Sorted list of top-level YAML key strings that represent domains.
        """
        # Filter out any top-level keys that don't represent domains
        # (e.g., 'version' is configuration metadata, not a domain)
        return sorted(
            k for k in self._rules 
            if isinstance(self._rules[k], dict)  # Domains should have dict configs
        )

    # ------------------------------------------------------------------
    # Domain evaluators
    # ------------------------------------------------------------------

    def _evaluate_sales(self, current: float, previous: float) -> RuleResult:
        """
        Apply sales rules to ``current`` and ``previous``.

        Rules evaluated (in priority order):
        1. ``sales.absolute_minimum`` — fires if ``current`` is below the
           absolute revenue floor, regardless of percentage change.
        2. ``sales.daily_drop_percent`` — fires if the percentage drop from
           ``previous`` to ``current`` exceeds the threshold.

        Args:
            current:  Current period sales value.
            previous: Prior period sales value.

        Returns:
            :class:`RuleResult` with trigger status and diagnostic details.

        Raises:
            ValueError: If required configuration keys are missing.
        """
        cfg = self._rules.get("sales", {})
        
        # Fail fast if required config is missing - no silent defaults
        if "daily_drop_percent" not in cfg:
            raise ValueError("Missing required config 'daily_drop_percent' in sales rules")
        if "absolute_minimum" not in cfg:
            raise ValueError("Missing required config 'absolute_minimum' in sales rules")
            
        daily_drop_threshold: float = float(cfg["daily_drop_percent"])
        absolute_minimum: float = float(cfg["absolute_minimum"])

        percent_drop = self._percent_change(current, previous)
        details: dict[str, Any] = {
            "current": current,
            "previous": previous,
            "percent_drop": round(percent_drop, 4),
            "daily_drop_threshold": daily_drop_threshold,
            "absolute_minimum": absolute_minimum,
        }

        # Rule 1: absolute floor — highest priority
        if current < absolute_minimum:
            return RuleResult(
                rule_triggered=True,
                rule_name="sales.absolute_minimum",
                details={
                    **details,
                    "breach": f"current ({current}) < absolute_minimum ({absolute_minimum})",
                },
            )

        # Rule 2: percentage drop
        if percent_drop > daily_drop_threshold:
            return RuleResult(
                rule_triggered=True,
                rule_name="sales.daily_drop_percent",
                details={
                    **details,
                    "breach": (
                        f"drop of {percent_drop:.2f}% "
                        f"exceeds threshold of {daily_drop_threshold}%"
                    ),
                },
            )

        return RuleResult(
            rule_triggered=False,
            rule_name=None,
            details=details,
        )

    def _evaluate_complaints(self, current: float, previous: float) -> RuleResult:
        """
        Apply complaints rules to ``current`` and ``previous``.

        Rule evaluated:
        - ``complaints.daily_increase_threshold`` — fires if
          ``current > previous * (1 + threshold / 100)``.

        Args:
            current:  Current period complaint count.
            previous: Prior period complaint count.

        Returns:
            :class:`RuleResult` with trigger status and diagnostic details.
            
        Raises:
            ValueError: If required configuration keys are missing.
        """
        cfg = self._rules.get("complaints", {})
        
        # Fail fast if required config is missing
        if "daily_increase_threshold" not in cfg:
            raise ValueError("Missing required config 'daily_increase_threshold' in complaints rules")
            
        threshold: float = float(cfg["daily_increase_threshold"])

        trigger_level = previous * (1 + threshold / 100.0)
        triggered = current > trigger_level

        details: dict[str, Any] = {
            "current": current,
            "previous": previous,
            "daily_increase_threshold": threshold,
            "trigger_level": round(trigger_level, 4),
        }

        if triggered:
            return RuleResult(
                rule_triggered=True,
                rule_name="complaints.daily_increase_threshold",
                details={
                    **details,
                    "breach": (
                        f"current ({current}) > trigger level "
                        f"({trigger_level:.2f}) "
                        f"[previous={previous} × (1 + {threshold}%)]"
                    ),
                },
            )

        return RuleResult(
            rule_triggered=False,
            rule_name=None,
            details=details,
        )

    def _evaluate_inventory(self, current: float, previous: float) -> RuleResult:
        """
        Apply inventory rules to ``current``.

        Rules evaluated (in priority order):
        1. ``inventory.critical_threshold`` — CRITICAL breach; fires first.
        2. ``inventory.low_stock_threshold`` — LOW-STOCK breach; fires only
           if the critical threshold was not triggered.

        ``previous`` is accepted for API consistency but is not used by
        inventory rules, which are absolute-value based.

        Args:
            current:  Current on-hand stock level (units).
            previous: Prior stock level (unused; accepted for API consistency).

        Returns:
            :class:`RuleResult` with trigger status and diagnostic details.
            
        Raises:
            ValueError: If required configuration keys are missing.
        """
        cfg = self._rules.get("inventory", {})
        
        # Fail fast if required config is missing
        if "critical_threshold" not in cfg:
            raise ValueError("Missing required config 'critical_threshold' in inventory rules")
        if "low_stock_threshold" not in cfg:
            raise ValueError("Missing required config 'low_stock_threshold' in inventory rules")
            
        critical_threshold: float = float(cfg["critical_threshold"])
        low_stock_threshold: float = float(cfg["low_stock_threshold"])

        details: dict[str, Any] = {
            "current": current,
            "previous": previous,
            "critical_threshold": critical_threshold,
            "low_stock_threshold": low_stock_threshold,
        }

        if current <= critical_threshold:
            return RuleResult(
                rule_triggered=True,
                rule_name="inventory.critical_threshold",
                details={
                    **details,
                    "breach": (
                        f"current ({current}) <= critical_threshold ({critical_threshold})"
                    ),
                },
            )

        if current <= low_stock_threshold:
            return RuleResult(
                rule_triggered=True,
                rule_name="inventory.low_stock_threshold",
                details={
                    **details,
                    "breach": (
                        f"current ({current}) <= low_stock_threshold ({low_stock_threshold})"
                    ),
                },
            )

        return RuleResult(
            rule_triggered=False,
            rule_name=None,
            details=details,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_rules(path: Path) -> dict[str, Any]:
        """
        Read and parse the YAML rules file.

        Args:
            path: Absolute path to the YAML file.

        Returns:
            Parsed rules as a :class:`dict` with non-domain keys (like 'version') removed.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError:        If the file is empty or does not parse to a dict.
        """
        if not path.exists():
            raise FileNotFoundError(
                f"Detection rules file not found at: '{path}'. "
                "Ensure 'aeam/config/detection_rules.yaml' exists."
            )

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if data is None:
            raise ValueError(
                f"Detection rules file at '{path}' is empty."
            )
        if not isinstance(data, dict):
            raise ValueError(
                f"Detection rules file at '{path}' must parse to a YAML mapping. "
                f"Got: {type(data).__name__!r}."
            )
        
        # Remove non-domain keys like 'version' that shouldn't be treated as domains
        data.pop("version", None)
        
        return data

    @staticmethod
    def _percent_change(current: float, previous: float) -> float:
        """
        Compute the percentage *drop* from ``previous`` to ``current``.

        A positive return value indicates ``current`` is lower than
        ``previous`` (a drop). A negative value indicates an increase.

        Returns ``0.0`` when ``previous`` is zero to avoid division by zero.

        Args:
            current:  The new value.
            previous: The baseline value.

        Returns:
            Percentage drop as a :class:`float` (e.g. ``15.0`` for a 15% drop).
        """
        if previous == 0.0:
            return 0.0
        # Removed abs() to maintain sign consistency
        return ((previous - current) / previous) * 100.0

    def __repr__(self) -> str:
        return (
            f"RuleEngine(domains={self.loaded_domains})"
        )

# Import yaml at the bottom to avoid circular imports
import yaml