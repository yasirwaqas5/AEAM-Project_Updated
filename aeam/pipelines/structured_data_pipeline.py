"""
aeam/pipelines/structured_data_pipeline.py

Structured Data Pipeline for KPI ingestion and cleaning in the AEAM monolith.

Provides deterministic, stateless transformations for structured KPI records:
schema validation, missing-value imputation, outlier winsorization, and
metric summarization. All operations use only the Python standard library —
no external ML or data-processing libraries.

This module:
- Makes no database calls.
- Creates no events.
- Performs no I/O.
- Is fully deterministic: identical inputs always produce identical outputs.
"""

from __future__ import annotations

import math
import statistics
from typing import Any


class StructuredDataPipeline:
    """
    Stateless pipeline for cleaning and summarizing structured KPI data.

    Each method is a self-contained transformation. They may be composed in
    sequence by calling code; this class imposes no ordering constraint.

    Example::

        pipeline = StructuredDataPipeline()

        if not pipeline.validate_schema(record, ["metric", "value", "timestamp"]):
            discard(record)
            return

        cleaned = pipeline.clean_missing(raw_values)
        winsorized = pipeline.handle_outliers(cleaned)
        summary = pipeline.summarize(current=winsorized[-1], expected=60.0, severity="HIGH")
    """

    # ------------------------------------------------------------------
    # Schema validation
    # ------------------------------------------------------------------

    def validate_schema(
        self,
        record: dict[str, Any],
        required_fields: list[str],
    ) -> bool:
        """
        Check that ``record`` contains all ``required_fields`` with non-None values.

        A field is considered present and valid if:
        - The key exists in ``record``, **and**
        - Its value is not ``None``.

        Empty strings and zero-valued numerics are considered valid — only
        explicit ``None`` is treated as absent.

        Args:
            record:          The input record dict to validate.
            required_fields: List of field names that must be present. An empty
                             list always returns ``True``.

        Returns:
            ``True`` if all required fields are present and non-None.
            ``False`` if any field is missing or ``None``.

        Example::

            pipeline.validate_schema(
                {"metric": "cpu", "value": 97.4, "timestamp": "2024-01-01"},
                required_fields=["metric", "value", "timestamp"],
            )
            # → True

            pipeline.validate_schema(
                {"metric": "cpu", "value": None},
                required_fields=["metric", "value", "timestamp"],
            )
            # → False  ("timestamp" missing, "value" is None)
        """
        if not required_fields:
            return True

        for field in required_fields:
            if field not in record or record[field] is None:
                return False
        return True

    # ------------------------------------------------------------------
    # Missing value imputation
    # ------------------------------------------------------------------

    def clean_missing(self, values: list[float]) -> list[float]:
        """
        Impute missing values (represented as ``float('nan')``) in ``values``.

        Imputation strategy:
        - **Single isolated NaN** → forward-fill from the preceding valid value.
          If no prior value exists, backward-fill from the next valid value.
        - **Multiple consecutive NaNs** → linear interpolation between the
          nearest valid neighbours. If one bound is missing (leading/trailing
          run), backward- or forward-fill from the available neighbour.
        - **All NaN** → returns a list of zeros of the same length (no valid
          anchor exists for any imputation strategy).

        The original list is never mutated; a new list is returned.

        Args:
            values: Time-ordered list of floats. Missing positions must be
                    encoded as ``float('nan')``. Non-NaN values are left
                    unchanged.

        Returns:
            A new list of the same length with all NaN positions filled.

        Example::

            pipeline.clean_missing([1.0, float('nan'), 3.0])
            # → [1.0, 2.0, 3.0]   (linear interpolation)

            pipeline.clean_missing([1.0, float('nan'), float('nan'), 4.0])
            # → [1.0, 2.0, 3.0, 4.0]   (linear interpolation across gap)

            pipeline.clean_missing([float('nan'), 2.0, 3.0])
            # → [2.0, 2.0, 3.0]   (backward-fill for leading NaN)
        """
        if not values:
            return []

        result: list[float] = list(values)
        n = len(result)

        # Check for all-NaN edge case.
        if all(math.isnan(v) for v in result):
            return [0.0] * n

        # Identify contiguous NaN runs as (start_idx, end_idx) inclusive spans.
        i = 0
        while i < n:
            if not math.isnan(result[i]):
                i += 1
                continue

            # Found the start of a NaN run — find its end.
            run_start = i
            while i < n and math.isnan(result[i]):
                i += 1
            run_end = i - 1  # inclusive

            # Find the nearest valid neighbours.
            left_val: float | None = None if run_start == 0 else result[run_start - 1]
            right_val: float | None = None if run_end == n - 1 else result[run_end + 1]

            run_len = run_end - run_start + 1

            if left_val is not None and right_val is not None:
                # Interpolate linearly between left and right anchors.
                # The gap spans (run_len + 1) segments between the two anchors.
                total_steps = run_len + 1
                for offset in range(run_len):
                    frac = (offset + 1) / total_steps
                    result[run_start + offset] = left_val + frac * (right_val - left_val)

            elif left_val is not None:
                # Trailing NaN run — forward-fill.
                for offset in range(run_len):
                    result[run_start + offset] = left_val

            else:
                # Leading NaN run — backward-fill (right_val is guaranteed non-None
                # because we handled all-NaN above).
                for offset in range(run_len):
                    result[run_start + offset] = right_val  # type: ignore[assignment]

        return result

    # ------------------------------------------------------------------
    # Outlier handling
    # ------------------------------------------------------------------

    def handle_outliers(self, values: list[float]) -> list[float]:
        """
        Winsorize ``values`` by capping at the 1st and 99th percentiles.

        Values below the 1st percentile are replaced with the 1st percentile
        value; values above the 99th percentile are replaced with the 99th
        percentile value. Values within the bounds are unchanged.

        Safe fallbacks:
        - Lists with fewer than 2 elements are returned as-is (no meaningful
          percentile can be computed).
        - NaN values are passed through unchanged (call :meth:`clean_missing`
          before this method in production pipelines).

        The original list is never mutated; a new list is returned.

        Args:
            values: List of numeric observations to winsorize.

        Returns:
            A new list of the same length with extreme values capped.

        Example::

            pipeline.handle_outliers([1.0, 2.0, 3.0, 4.0, 1000.0])
            # → [1.0, 2.0, 3.0, 4.0, ~4.96]  (1000.0 capped at p99)
        """
        if len(values) < 2:
            return list(values)

        # Separate NaN from valid for percentile computation.
        valid = [v for v in values if not math.isnan(v)]
        if not valid:
            return list(values)

        p1 = self._interpolated_percentile(sorted(valid), 1)
        p99 = self._interpolated_percentile(sorted(valid), 99)

        return [
            v if math.isnan(v) else max(p1, min(p99, v))
            for v in values
        ]

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    def summarize(
        self,
        current: float,
        expected: float,
        severity: str,
    ) -> dict[str, Any]:
        """
        Produce a metric summary comparing ``current`` to ``expected``.

        Computes the absolute deviation, percentage deviation, and a direction
        label. ``expected`` of zero is handled safely (percentage deviation
        reported as ``None``).

        Args:
            current:  The observed metric value.
            expected: The baseline or forecast value to compare against.
            severity: Severity label string (e.g. ``"HIGH"``, ``"CRITICAL"``).
                      Passed through verbatim; not validated here.

        Returns:
            A :class:`dict` with the following structure::

                {
                    "current":            float,
                    "expected":           float,
                    "absolute_deviation": float,
                    "percent_deviation":  float | None,
                    "direction":          "above" | "below" | "equal",
                    "severity":           str,
                }

            ``percent_deviation`` is ``None`` when ``expected == 0`` (undefined).
            ``direction`` is ``"equal"`` when ``current == expected``.

        Example::

            pipeline.summarize(current=42_000.0, expected=55_000.0, severity="HIGH")
            # → {
            #     "current": 42000.0,
            #     "expected": 55000.0,
            #     "absolute_deviation": -13000.0,
            #     "percent_deviation": -23.636...,
            #     "direction": "below",
            #     "severity": "HIGH",
            #   }
        """
        absolute_deviation = current - expected

        if expected != 0.0:
            percent_deviation: float | None = (absolute_deviation / abs(expected)) * 100.0
        else:
            percent_deviation = None

        if current > expected:
            direction = "above"
        elif current < expected:
            direction = "below"
        else:
            direction = "equal"

        return {
            "current": current,
            "expected": expected,
            "absolute_deviation": round(absolute_deviation, 6),
            "percent_deviation": (
                round(percent_deviation, 6) if percent_deviation is not None else None
            ),
            "direction": direction,
            "severity": severity,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _interpolated_percentile(sorted_vals: list[float], percentile: int) -> float:
        """
        Compute an interpolated percentile from a pre-sorted list.

        Uses linear interpolation matching ``numpy.percentile(method='linear')``.

        Args:
            sorted_vals: Pre-sorted list of floats (ascending, no NaN).
            percentile:  Target percentile (0–100 inclusive).

        Returns:
            Interpolated percentile value as a :class:`float`.
        """
        n = len(sorted_vals)
        if n == 0:
            return 0.0
        if percentile == 0:
            return float(sorted_vals[0])
        if percentile == 100:
            return float(sorted_vals[-1])

        idx = (percentile / 100.0) * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        frac = idx - lo

        if lo == hi:
            return float(sorted_vals[lo])
        return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac

    def __repr__(self) -> str:
        return "StructuredDataPipeline()"