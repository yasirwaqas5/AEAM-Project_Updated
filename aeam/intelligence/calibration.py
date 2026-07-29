"""
aeam/intelligence/calibration.py

Confidence recalibration engine (Phase F2 — Adaptive Learning, Feedback
Loop & Confidence Recalibration).

The problem this solves
-----------------------
AEAM has always produced a confidence number, and that number has never
meant anything checkable. A "0.8" should resolve correctly about 80% of the
time; nothing ever verified that it did. Until it is verified, confidence
cannot be used to route approvals or drive automation thresholds, because
nobody knows what threshold corresponds to what risk.

This module turns confidence into a measured quantity. It:

* extracts **labeled signal** from records the platform already keeps —
  E9 human verdicts (approved/rejected) and resolved-incident outcomes;
* fits a monotone mapping from stated confidence to observed resolution
  rate (isotonic regression via pool-adjacent-violators);
* measures how far the confidence curve sits from the diagonal, before and
  after, on data the fit never saw.

Everything here is **pure**: no database, no network, no LLM, no global
state. Records arrive as plain dicts from the caller (``LearningAgent``),
which owns all I/O. That separation is what makes calibration testable
against a fixture instead of against a deployment.

Why isotonic and not Platt
--------------------------
Platt scaling fits a sigmoid, which assumes the miscalibration has a
particular shape. AEAM's confidence is assembled additively from
independent components (detector agreement, deviation magnitude,
persistence, history depth — see the F1 KPI Agent), and there is no reason
to expect its error to be sigmoidal. Isotonic assumes only **monotonicity**
— higher stated confidence should not mean lower actual resolution — which
is the one property the score genuinely has to have. It is also
implemented here in ~40 lines of stdlib rather than pulling scikit-learn
into the finalize path, matching the constitutional precedent that
implemented BM25 in stdlib rather than adopting a retrieval framework
(TECH-1/TECH-2).

The cost of isotonic is that it overfits on small samples, which is why
``MIN_TRAINING_SAMPLES`` exists and why the engine refuses to produce a
mapping below it rather than producing a confident-looking bad one.

Honesty contract
----------------
* A calibration that does not measurably improve on held-out data is
  reported as such, not shipped (PHIL-1: calibration is measured, never
  asserted).
* ECE is reported with its bucket count and sample size, because an ECE
  computed over 12 samples is not comparable to one over 1,200 (OBS-2).
* Applying a mapping never discards the raw value (EXPL-4: adjustments are
  disclosed with their magnitude and reason).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Engine-owned defaults (ENG-6).
#
# Below this many labeled samples isotonic regression fits noise: with 30
# points it will happily produce a step function that reproduces the
# training set exactly and generalises to nothing. The engine refuses
# rather than shipping it.
MIN_TRAINING_SAMPLES: int = 60

# Held-out fraction. The fit never sees these; every reported improvement
# is measured on them. 0.3 keeps enough training signal at the minimum
# sample size while leaving a holdout big enough to be worth reporting.
DEFAULT_HOLDOUT_FRACTION: float = 0.3

# Buckets for the calibration curve and ECE. Ten gives 0.1-wide bins, the
# conventional reliability-diagram resolution, and keeps per-bucket counts
# meaningful at the sample sizes this platform realistically produces.
DEFAULT_BUCKETS: int = 10

# An improvement smaller than this is noise, not learning. A recalibration
# that only clears this by a hair is reported as "not improved" so nobody
# ships a mapping on the strength of a rounding difference.
MIN_ECE_IMPROVEMENT: float = 0.01


# ---------------------------------------------------------------------------
# Labeled signal
# ---------------------------------------------------------------------------
#
# Two sources, in priority order. A human verdict is stronger evidence than
# a derived status: a reviewer who rejected an incident's analysis has said
# something the pipeline's own status vocabulary cannot express.

#: E9 verdicts that count as "the analysis was correct".
POSITIVE_VERDICTS: frozenset[str] = frozenset({"approved"})

#: E9 verdicts that count as "the analysis was not correct".
NEGATIVE_VERDICTS: frozenset[str] = frozenset({"rejected"})

#: Verdicts that carry no outcome signal. ``changes_requested`` and
#: ``escalated`` mean the review is still in motion — treating either as a
#: negative would punish confidence for a decision nobody has made yet.
NEUTRAL_VERDICTS: frozenset[str] = frozenset({"changes_requested", "escalated"})

#: Investigation statuses that indicate the platform reached a real answer.
POSITIVE_STATUSES: frozenset[str] = frozenset({"RESOLVED"})

#: Statuses indicating it did not. ESCALATED is deliberately ABSENT from
#: both sets: escalation means a human was asked, not that the analysis was
#: wrong, and scoring it as a failure would train the platform to be
#: under-confident precisely on the incidents that matter most.
NEGATIVE_STATUSES: frozenset[str] = frozenset({"FAILED"})


@dataclass
class LabeledSample:
    """One (predicted confidence, observed outcome) pair with its provenance."""

    incident_id: str
    confidence: float
    outcome: bool
    source: str  # "verdict" | "status"

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "confidence": self.confidence,
            "outcome": self.outcome,
            "source": self.source,
        }


def extract_labeled_samples(
    incidents: list[dict[str, Any]],
    verdicts: list[dict[str, Any]] | None = None,
) -> tuple[list[LabeledSample], dict[str, int]]:
    """Turn persisted records into labeled training signal.

    Read-only in the strongest sense: the inputs are plain dicts and
    nothing here writes anywhere. MEM-2 (past incidents are never mutated)
    is satisfied structurally — this function has nothing to mutate.

    Args:
        incidents: Incident rows. Each needs ``incident_id``, ``confidence``,
                   and ``investigation_status`` (or enough to have derived
                   it). Rows missing a usable confidence are skipped and
                   counted, never defaulted to a number nobody predicted.
        verdicts:  E9 ``review_verdicts`` rows. When an incident has a
                   verdict, the verdict wins — a human's judgement is
                   stronger signal than a derived status.

    Returns:
        ``(samples, skipped)`` where ``skipped`` counts each exclusion
        reason. The counts are returned rather than logged away because
        "we trained on 200 of 1,000 incidents" is something the operator
        approving a calibration needs to see.
    """
    verdict_by_incident: dict[str, str] = {}
    for row in verdicts or []:
        incident_id = str(row.get("incident_id") or "").strip()
        verdict = str(row.get("verdict") or "").strip().lower()
        if not incident_id or not verdict:
            continue
        # Latest verdict wins; rows arrive oldest-first by query contract.
        verdict_by_incident[incident_id] = verdict

    samples: list[LabeledSample] = []
    skipped = {
        "no_confidence": 0,
        "confidence_out_of_range": 0,
        "neutral_verdict": 0,
        "no_outcome_signal": 0,
    }

    for row in incidents:
        incident_id = str(row.get("incident_id") or "").strip()
        raw_confidence = row.get("confidence")

        if raw_confidence is None:
            skipped["no_confidence"] += 1
            continue
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            skipped["no_confidence"] += 1
            continue
        if not 0.0 <= confidence <= 1.0:
            # A confidence outside [0,1] is a bug somewhere upstream, not a
            # training sample. Counted so it is visible rather than quietly
            # clipped into the range and learned from.
            skipped["confidence_out_of_range"] += 1
            continue

        verdict = verdict_by_incident.get(incident_id)
        if verdict in POSITIVE_VERDICTS:
            samples.append(LabeledSample(incident_id, confidence, True, "verdict"))
            continue
        if verdict in NEGATIVE_VERDICTS:
            samples.append(LabeledSample(incident_id, confidence, False, "verdict"))
            continue
        if verdict in NEUTRAL_VERDICTS:
            skipped["neutral_verdict"] += 1
            continue

        status = str(row.get("investigation_status") or "").strip().upper()
        if status in POSITIVE_STATUSES:
            samples.append(LabeledSample(incident_id, confidence, True, "status"))
        elif status in NEGATIVE_STATUSES:
            samples.append(LabeledSample(incident_id, confidence, False, "status"))
        else:
            skipped["no_outcome_signal"] += 1

    return samples, skipped


# ---------------------------------------------------------------------------
# Calibration quality
# ---------------------------------------------------------------------------


@dataclass
class CalibrationCurve:
    """A reliability diagram: what was predicted vs what actually happened."""

    buckets: list[dict[str, Any]]
    ece: float
    brier: float
    samples: int
    bucket_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "buckets": self.buckets,
            "ece": self.ece,
            "brier": self.brier,
            "samples": self.samples,
            # OBS-2: an ECE is meaningless without the resolution it was
            # computed at and the sample size behind it.
            "bucket_count": self.bucket_count,
        }


def calibration_curve(
    predictions: list[float],
    outcomes: list[bool],
    buckets: int = DEFAULT_BUCKETS,
) -> CalibrationCurve:
    """Measure how far stated confidence sits from observed reality.

    ECE (expected calibration error) is the sample-weighted mean distance
    between each bucket's mean prediction and its actual success rate — the
    distance from the diagonal of a reliability diagram, which is precisely
    what F2's acceptance criterion asks to be reduced.

    Brier score accompanies it as a proper scoring rule: ECE alone can be
    gamed by a model that predicts the base rate for everything (perfectly
    calibrated, entirely uninformative), and Brier punishes exactly that.

    Args:
        predictions: Stated confidences in [0, 1].
        outcomes:    Observed booleans, positionally aligned.
        buckets:     Reliability-diagram resolution.

    Returns:
        A :class:`CalibrationCurve`. Empty input yields zeroed metrics with
        ``samples=0`` — callers must check the count rather than reading a
        0.0 ECE as perfect calibration.

    Raises:
        ValueError: If the sequences differ in length, or ``buckets`` < 1.
                    Misalignment would silently score the wrong pairs.
    """
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions ({len(predictions)}) and outcomes ({len(outcomes)}) "
            "must be the same length."
        )
    if buckets < 1:
        raise ValueError("buckets must be >= 1.")

    if not predictions:
        return CalibrationCurve(buckets=[], ece=0.0, brier=0.0, samples=0, bucket_count=buckets)

    edges = [i / buckets for i in range(buckets + 1)]
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(buckets)]

    for prediction, outcome in zip(predictions, outcomes):
        # The final bucket is closed on the right so a prediction of exactly
        # 1.0 lands somewhere instead of falling off the end.
        index = min(int(prediction * buckets), buckets - 1)
        grouped[index].append((prediction, bool(outcome)))

    total = len(predictions)
    ece = 0.0
    bucket_rows: list[dict[str, Any]] = []

    for index, group in enumerate(grouped):
        if not group:
            bucket_rows.append({
                "range": [round(edges[index], 4), round(edges[index + 1], 4)],
                "count": 0,
                "mean_predicted": None,
                "actual_rate": None,
                "gap": None,
            })
            continue

        mean_predicted = sum(p for p, _ in group) / len(group)
        actual_rate = sum(1 for _, o in group if o) / len(group)
        gap = abs(mean_predicted - actual_rate)
        ece += (len(group) / total) * gap

        bucket_rows.append({
            "range": [round(edges[index], 4), round(edges[index + 1], 4)],
            "count": len(group),
            "mean_predicted": round(mean_predicted, 6),
            "actual_rate": round(actual_rate, 6),
            "gap": round(gap, 6),
        })

    brier = sum((p - (1.0 if o else 0.0)) ** 2 for p, o in zip(predictions, outcomes)) / total

    return CalibrationCurve(
        buckets=bucket_rows,
        ece=round(ece, 6),
        brier=round(brier, 6),
        samples=total,
        bucket_count=buckets,
    )


# ---------------------------------------------------------------------------
# Isotonic regression (pool adjacent violators)
# ---------------------------------------------------------------------------


def fit_isotonic(
    predictions: list[float],
    outcomes: list[bool],
) -> list[tuple[float, float]]:
    """Fit a monotone non-decreasing mapping from confidence to outcome rate.

    Pool-adjacent-violators, run over **distinct** x values with weights.

    Tie handling is the whole difficulty here, not a detail. AEAM's
    confidence is heavily discretized — assembled from a handful of
    additive components and rounded to two decimals — so a training set of
    400 incidents routinely contains only a dozen distinct values, each
    repeated dozens of times. Running PAV over raw samples treats each
    repetition as its own block; the blocks then share an x coordinate, the
    knot list becomes multivalued at that x, and interpolation reads
    whichever one it happens to hit. Measured on an overconfident fixture
    that produced ``apply(0.9) -> 1.0`` where the observed rate was 0.55 —
    a calibration that made the platform *more* overconfident.

    Pooling identical x values into one weighted point first makes the
    result single-valued and correct by construction.

    Args:
        predictions: Stated confidences.
        outcomes:    Observed booleans, positionally aligned.

    Returns:
        Knots as ``[(x, y), ...]`` with strictly increasing ``x``, ready for
        :func:`apply_calibration`. Empty input returns an empty list, which
        that function treats as "no mapping" and passes values through.

    Raises:
        ValueError: If the sequences differ in length.
    """
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must be the same length.")
    if not predictions:
        return []

    # Step 1: pool ties. {x: [sum_y, weight]}
    pooled: dict[float, list[float]] = {}
    for x, outcome in zip(predictions, outcomes):
        entry = pooled.setdefault(float(x), [0.0, 0.0])
        entry[0] += 1.0 if outcome else 0.0
        entry[1] += 1.0

    xs = sorted(pooled)

    # Step 2: weighted PAV. Each block is [sum_y, weight, x_index_start,
    # x_index_end] over the sorted distinct x values.
    blocks: list[list[float]] = []
    for index, x in enumerate(xs):
        sum_y, weight = pooled[x]
        blocks.append([sum_y, weight, index, index])
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            last = blocks.pop()
            previous = blocks.pop()
            blocks.append([
                previous[0] + last[0],
                previous[1] + last[1],
                previous[2],
                last[3],
            ])

    # Step 3: one knot per distinct x, carrying its block's pooled value.
    # Emitting per-x rather than per-block keeps x strictly increasing, so
    # apply_calibration's interpolation is unambiguous everywhere.
    knots: list[tuple[float, float]] = []
    for block in blocks:
        value = round(block[0] / block[1], 6)
        for index in range(int(block[2]), int(block[3]) + 1):
            knots.append((round(xs[index], 6), value))

    return knots


def apply_calibration(confidence: float, knots: list[tuple[float, float]]) -> float:
    """Map a raw confidence through a fitted isotonic mapping.

    Linear interpolation between knots, clamped to the end values outside
    the fitted range. Clamping rather than extrapolating is deliberate: the
    fit has no evidence beyond its data, and extrapolating a step function
    would invent a number the training set never supported.

    Args:
        confidence: Raw confidence in [0, 1].
        knots:      Output of :func:`fit_isotonic`. Empty passes through.

    Returns:
        The calibrated confidence, clamped to [0, 1] and rounded to 4
        decimals to match the precision confidence is stored at elsewhere.
    """
    if not knots:
        return confidence

    if confidence <= knots[0][0]:
        return round(max(0.0, min(1.0, knots[0][1])), 4)
    if confidence >= knots[-1][0]:
        return round(max(0.0, min(1.0, knots[-1][1])), 4)

    for index in range(1, len(knots)):
        x1, y1 = knots[index - 1]
        x2, y2 = knots[index]
        if confidence <= x2:
            if x2 == x1:
                return round(max(0.0, min(1.0, y2)), 4)
            ratio = (confidence - x1) / (x2 - x1)
            return round(max(0.0, min(1.0, y1 + ratio * (y2 - y1))), 4)

    return round(max(0.0, min(1.0, knots[-1][1])), 4)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass
class CalibrationFit:
    """The outcome of a recalibration attempt, including a refusal."""

    knots: list[tuple[float, float]]
    improved: bool
    reason: str | None
    training_samples: int
    holdout_samples: int
    before: CalibrationCurve | None = None
    after: CalibrationCurve | None = None
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """True when a mapping was produced AND measurably improved.

        Callers must check this rather than testing ``knots`` — a fit that
        produced knots but failed to improve on held-out data must not be
        shipped, and treating "we computed something" as success is exactly
        the assertion-instead-of-measurement PHIL-1 forbids.
        """
        return bool(self.knots) and self.improved

    def to_dict(self) -> dict[str, Any]:
        return {
            "knots": [list(k) for k in self.knots],
            "improved": self.improved,
            "reason": self.reason,
            "training_samples": self.training_samples,
            "holdout_samples": self.holdout_samples,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "skipped": dict(self.skipped),
            "ece_before": self.before.ece if self.before else None,
            "ece_after": self.after.ece if self.after else None,
            "brier_before": self.before.brier if self.before else None,
            "brier_after": self.after.brier if self.after else None,
        }


class CalibrationEngine:
    """Fits and evaluates confidence recalibration mappings.

    Pure: no I/O of any kind. ``LearningAgent`` supplies records and
    persists results; this class only computes.

    Args:
        min_training_samples: Refuse to fit below this many labeled samples.
        holdout_fraction:     Fraction withheld from the fit and used to
                              measure improvement.
        buckets:              Reliability-diagram resolution.
        min_improvement:      Minimum ECE reduction on holdout that counts
                              as real.

    Raises:
        ValueError: If any parameter is outside its usable range.
    """

    def __init__(
        self,
        min_training_samples: int = MIN_TRAINING_SAMPLES,
        holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
        buckets: int = DEFAULT_BUCKETS,
        min_improvement: float = MIN_ECE_IMPROVEMENT,
    ) -> None:
        if min_training_samples < 2:
            raise ValueError("min_training_samples must be >= 2.")
        if not 0.0 < holdout_fraction < 1.0:
            raise ValueError("holdout_fraction must be in (0, 1).")
        if buckets < 1:
            raise ValueError("buckets must be >= 1.")
        if min_improvement < 0:
            raise ValueError("min_improvement must be >= 0.")

        self._min_samples = min_training_samples
        self._holdout_fraction = holdout_fraction
        self._buckets = buckets
        self._min_improvement = min_improvement

    def fit(self, samples: list[LabeledSample], skipped: dict[str, int] | None = None) -> CalibrationFit:
        """Fit a calibration mapping and measure it on held-out data.

        The split is **deterministic** — every third sample by index, given
        a 0.3 holdout — rather than randomised. A random split would make a
        recalibration unreproducible: an operator could not re-run it and
        get the same numbers, and a governance decision that cannot be
        reproduced is not auditable.

        Args:
            samples: Labeled signal from :func:`extract_labeled_samples`.
            skipped: Exclusion counts to carry into the result for the
                     operator reviewing the recalibration.

        Returns:
            A :class:`CalibrationFit`. Check :attr:`CalibrationFit.usable`
            before shipping the mapping.
        """
        skipped = dict(skipped or {})

        if len(samples) < self._min_samples:
            return CalibrationFit(
                knots=[], improved=False,
                reason=(
                    f"{len(samples)} labeled samples available; "
                    f"{self._min_samples} required. Isotonic regression on fewer "
                    "reproduces the training set and generalises to nothing."
                ),
                training_samples=len(samples), holdout_samples=0, skipped=skipped,
            )

        # Deterministic interleaved split: preserves the confidence
        # distribution across both sides without needing a shuffle.
        stride = max(2, round(1 / self._holdout_fraction))
        train = [s for i, s in enumerate(samples) if i % stride != 0]
        holdout = [s for i, s in enumerate(samples) if i % stride == 0]

        if not holdout or not train:
            return CalibrationFit(
                knots=[], improved=False,
                reason="The train/holdout split left one side empty.",
                training_samples=len(train), holdout_samples=len(holdout), skipped=skipped,
            )

        outcomes = {s.outcome for s in train}
        if len(outcomes) < 2:
            # Every training outcome identical. Isotonic would map every
            # confidence to a constant — technically "perfectly calibrated"
            # on that data and catastrophic in production.
            only = "successes" if outcomes == {True} else "failures"
            return CalibrationFit(
                knots=[], improved=False,
                reason=(
                    f"Every training sample is a {only}; a mapping fitted on one "
                    "outcome class collapses all confidences to a constant."
                ),
                training_samples=len(train), holdout_samples=len(holdout), skipped=skipped,
            )

        knots = fit_isotonic([s.confidence for s in train], [s.outcome for s in train])

        holdout_raw = [s.confidence for s in holdout]
        holdout_outcomes = [s.outcome for s in holdout]
        holdout_calibrated = [apply_calibration(c, knots) for c in holdout_raw]

        before = calibration_curve(holdout_raw, holdout_outcomes, buckets=self._buckets)
        after = calibration_curve(holdout_calibrated, holdout_outcomes, buckets=self._buckets)

        improvement = before.ece - after.ece
        improved = improvement >= self._min_improvement

        reason = None
        if not improved:
            reason = (
                f"Held-out ECE moved {before.ece:.6f} -> {after.ece:.6f} "
                f"({improvement:+.6f}), below the {self._min_improvement} threshold "
                "that distinguishes learning from noise. Calibration not adopted."
            )

        logger.info(
            "CalibrationEngine.fit | train=%d | holdout=%d | ece %.6f -> %.6f | improved=%s",
            len(train), len(holdout), before.ece, after.ece, improved,
        )

        return CalibrationFit(
            knots=knots,
            improved=improved,
            reason=reason,
            training_samples=len(train),
            holdout_samples=len(holdout),
            before=before,
            after=after,
            skipped=skipped,
        )

    def __repr__(self) -> str:
        return (
            f"CalibrationEngine(min_training_samples={self._min_samples}, "
            f"holdout_fraction={self._holdout_fraction}, buckets={self._buckets})"
        )
