"""
aeam/agents/kpi/composite_rule_engine.py

CompositeRuleEngine — dynamic metric-domain composition (Phase B1.7).

Composes a base :class:`~aeam.agents.kpi.rule_engine.RuleEngine` (unchanged,
never subclassed or modified) with any number of dynamic domain providers, and
exposes the exact same duck-typed interface ``MonitorAgent`` already depends
on: a ``loaded_domains`` property and an ``evaluate(metric_name, current,
previous)`` method. ``MonitorAgent`` is never modified — it is simply handed
this object instead of a bare ``RuleEngine()`` at construction time (see
``aeam/main.py``).

Why ``evaluate()`` needs zero conditional logic
------------------------------------------------
``RuleEngine.evaluate()`` already returns a graceful, non-triggered result for
any metric name it doesn't recognise (a "no rules configured for metric
domain" reason, sourced from its own code today — see ``rule_engine.py``).
That fallback exists for every unrecognised name whether it originated from a
curated domain typo or a brand-new dynamic metric; this class does not need
to distinguish the two. ``evaluate()`` is therefore a pure, unconditional
passthrough to the base engine — which also makes it provably
non-regressing for the three curated domains (``sales``/``complaints``/
``inventory``): the exact same object receives the exact same call it always
has.

Only ``loaded_domains`` needs augmentation — the single gate
``MonitorAgent._run_cycle`` uses to decide which metric names to evaluate
each cycle at all.

Generic by construction (no dataset-specific code here)
---------------------------------------------------------
A "domain provider" is any ``Callable[[], list[str]]`` — mirroring the same
composition idiom already used by
:class:`~aeam.connectors.composite_kpi_source.CompositeKPISource`
(``add_multi``'s ``selectors`` callable). This class has no knowledge of
"datasets"; ``aeam/main.py`` registers a dataset-backed provider by supplying
a small closure over ``DatasetIntelligenceService.list_monitorable_metric_names``.
Any future connector (PostgreSQL, Snowflake, REST, ...) participates the same
way: register a provider returning its own monitorable metric names — no
change to this class is ever required.
"""

from __future__ import annotations

import logging
from typing import Callable

from aeam.agents.kpi.rule_engine import RuleEngine, RuleOutput

logger = logging.getLogger(__name__)

#: A domain provider: returns the list of metric names it currently wants
#: monitored. Called fresh on every ``loaded_domains`` access — never cached
#: here — so activation changes are picked up on the very next cycle.
DomainProvider = Callable[[], list[str]]


class CompositeRuleEngine:
    """
    Wraps a base ``RuleEngine`` with dynamically-discovered metric domains.

    Args:
        base: The real, unmodified :class:`~aeam.agents.kpi.rule_engine.RuleEngine`
              instance whose curated domains and thresholds are preserved
              exactly. Never mutated, subclassed, or replaced by this class.

    Raises:
        ValueError: If ``base`` is ``None``.

    Example::

        engine = CompositeRuleEngine(base=RuleEngine())
        engine.add_domain_provider(
            "datasets",
            lambda: dataset_intelligence.list_monitorable_metric_names(
                dataset_activation.list_activated_dataset_ids()
            ),
        )
        monitor_agent = MonitorAgent(..., rule_engine=engine)
    """

    def __init__(self, base: RuleEngine) -> None:
        if base is None:
            raise ValueError("base must not be None.")
        self._base = base
        self._providers: list[tuple[str, DomainProvider]] = []

    # ------------------------------------------------------------------
    # Composition (fluent builder — used once at bootstrap)
    # ------------------------------------------------------------------

    def add_domain_provider(self, name: str, provider: DomainProvider) -> "CompositeRuleEngine":
        """
        Register a dynamic domain provider.

        Args:
            name:     Short label used only in log messages if ``provider``
                      raises (e.g. ``"datasets"``).
            provider: Zero-arg callable returning the metric names this
                      provider currently wants monitored. Called fresh on
                      every :attr:`loaded_domains` access.

        Returns:
            ``self``, for fluent chaining.
        """
        self._providers.append((name, provider))
        return self

    @property
    def provider_count(self) -> int:
        """Number of registered dynamic domain providers."""
        return len(self._providers)

    # ------------------------------------------------------------------
    # RuleEngine-shaped interface (duck-typed; MonitorAgent unchanged)
    # ------------------------------------------------------------------

    @property
    def loaded_domains(self) -> list[str]:
        """
        Union of the base engine's curated domains and every registered
        provider's current names — sorted, de-duplicated, recomputed fresh on
        every access (no caching), so a newly activated source's metrics
        appear on the very next ``MonitorAgent`` cycle with no restart.

        A failing provider is logged and skipped — never raised — so one bad
        provider can never break domain discovery for the rest, or crash a
        monitoring cycle.
        """
        domains: set[str] = set(self._base.loaded_domains)
        for name, provider in self._providers:
            try:
                domains.update(provider())
            except Exception as exc:  # noqa: BLE001 - one bad provider must not break the cycle
                logger.error(
                    "CompositeRuleEngine | domain provider %r failed: %s", name, exc, exc_info=True,
                )
                continue
        return sorted(domains)

    def evaluate(self, metric_name: str, current: float, previous: float) -> RuleOutput:
        """
        Pure passthrough to the base engine — see module docstring for why no
        branching is needed here. Curated domains get an identical result to
        calling the base engine directly; any other name gets the base
        engine's own existing graceful "no rules configured" fallback.
        """
        return self._base.evaluate(metric_name=metric_name, current=current, previous=previous)

    def __repr__(self) -> str:
        return f"CompositeRuleEngine(providers={self.provider_count})"
