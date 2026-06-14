"""
aeam/agents/kpi/statistical_detector.py

Deterministic statistical anomaly detection for the AEAM KPI Agent.

Pure, rolling-window-based statistical detector.
No I/O, no external libraries, no state mutation.
"""

import math
import statistics
from typing import NamedTuple


class DetectionResult(NamedTuple):
    moving_avg: float
    z_score: float
    percentile_low: float
    percentile_high: float
    statistical_anomaly: bool

    def to_dict(self) -> dict[str, float | bool]:
        return {
            "moving_avg": self.moving_avg,
            "z_score": self.z_score,
            "percentile_low": self.percentile_low,
            "percentile_high": self.percentile_high,
            "statistical_anomaly": self.statistical_anomaly,
        }


class StatisticalDetector:
    """
    Rolling-window deterministic statistical anomaly detector.
    """

    Z_SCORE_THRESHOLD: float = 3.0

    def __init__(self, window_size: int = 7) -> None:
        if window_size < 1:
            raise ValueError("window_size must be >= 1.")
        self._window_size = window_size

    # -------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------

    def detect(
        self,
        current: float,
        history: list[float],
    ) -> dict[str, float | bool]:

        if not history:
            return DetectionResult(
                moving_avg=current,
                z_score=0.0,
                percentile_low=current,
                percentile_high=current,
                statistical_anomaly=False,
            ).to_dict()

        window = history[-self._window_size:]
        window = self._winsorize(window)

        moving_avg = statistics.mean(window)

        z = self._z_score(current, window)

        p_low, p_high = self._percentile_bounds(window)

        z_anomaly = abs(z) > self.Z_SCORE_THRESHOLD
        bounds_anomaly = current < p_low or current > p_high

        return DetectionResult(
            moving_avg=round(moving_avg, 6),
            z_score=round(z, 6),
            percentile_low=round(p_low, 6),
            percentile_high=round(p_high, 6),
            statistical_anomaly=(z_anomaly or bounds_anomaly),
        ).to_dict()

    # -------------------------------------------------------------
    # Internal Logic
    # -------------------------------------------------------------

    @staticmethod
    def _z_score(current: float, history: list[float]) -> float:
        if len(history) < 2:
            return 0.0

        mean = statistics.mean(history)
        try:
            stdev = statistics.stdev(history)
        except statistics.StatisticsError:
            return 0.0

        if stdev == 0.0:
            return 0.0

        return (current - mean) / stdev

    @staticmethod
    def _percentile_bounds(
        values: list[float],
        lower: int = 5,
        upper: int = 95,
    ) -> tuple[float, float]:

        if not 0 <= lower < upper <= 100:
            raise ValueError("Invalid percentile bounds.")

        if len(values) == 1:
            v = float(values[0])
            return v, v

        sorted_vals = sorted(values)
        n = len(sorted_vals)

        def percentile(p: int) -> float:
            if p == 0:
                return float(sorted_vals[0])
            if p == 100:
                return float(sorted_vals[-1])

            idx = (p / 100.0) * (n - 1)
            lo = int(math.floor(idx))
            hi = int(math.ceil(idx))
            frac = idx - lo

            if lo == hi:
                return float(sorted_vals[lo])

            return (
                float(sorted_vals[lo]) * (1 - frac)
                + float(sorted_vals[hi]) * frac
            )

        return percentile(lower), percentile(upper)

    @staticmethod
    def _winsorize(values: list[float], limit: float = 0.05) -> list[float]:
        """
        Winsorize extreme values at given percentage limit.
        Default: 5% on both ends.
        """
        if len(values) < 3:
            return values.copy()

        sorted_vals = sorted(values)
        n = len(sorted_vals)

        lower_idx = int(n * limit)
        upper_idx = int(n * (1 - limit)) - 1

        lower_bound = sorted_vals[lower_idx]
        upper_bound = sorted_vals[upper_idx]

        return [
            min(max(v, lower_bound), upper_bound)
            for v in values
        ]

    def __repr__(self) -> str:
        return f"StatisticalDetector(window_size={self._window_size})"