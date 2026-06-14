"""
aeam/pipelines/forecast_data_pipeline.py

Preprocessing pipeline for historical time-series data used in AEAM forecasting.

Prepares a pandas DataFrame with ``ds`` (datetime) and ``y`` (float) columns
for downstream forecasting models. Handles missing values, outliers, and
feature engineering. Never trains a model or calls an LLM.

Architecture constraints:
- No model training.
- No LLM calls.
- No new Event types.
- No external schedulers or APIs.
- All dependencies injected where applicable.
"""

from __future__ import annotations

import logging
import math

import pandas as pd

logger = logging.getLogger(__name__)

# Winsorization percentile bounds (matches StructuredDataPipeline convention).
_LOWER_PCT: float = 1.0
_UPPER_PCT: float = 99.0


class ForecastDataPipeline:
    """
    Preprocessing pipeline for time-series DataFrames.

    Accepts a raw ``pandas.DataFrame`` with at minimum a ``ds`` (datetime)
    column and a ``y`` (numeric) column, and produces a clean, feature-enriched
    DataFrame ready for a forecasting model.

    Three public methods are exposed:
    - :meth:`validate`          — clean ``ds`` / ``y``, impute, winsorise.
    - :meth:`feature_engineering` — add calendar features.
    - :meth:`prepare`           — run both in sequence (primary entry point).

    Constraints:
    - Does not train any model.
    - Makes no LLM calls.
    - Performs no database I/O.
    - Never mutates the input DataFrame — always returns a copy.

    Example::

        pipeline = ForecastDataPipeline()
        clean_df = pipeline.prepare(raw_df)
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Validate and clean the raw time-series DataFrame.

        Operations (in order):
        1. Assert required columns ``ds`` and ``y`` are present.
        2. Cast ``ds`` to ``datetime64`` and strip timezone info (timezone-naive).
        3. Cast ``y`` to ``float64``.
        4. Drop rows where ``ds`` is NaT.
        5. Impute missing ``y`` values:
           - Single isolated NaN → forward-fill from the preceding valid value
             (backward-fill if no prior value exists).
           - Multiple consecutive NaNs → linear interpolation between the
             nearest valid neighbours; forward/backward-fill at boundaries.
        6. Winsorise ``y`` at the 1st and 99th percentiles (cap outliers).
        7. Sort by ``ds`` ascending.
        8. Reset the integer index.

        Args:
            df: Raw input DataFrame. Must contain ``ds`` and ``y`` columns.
                The original DataFrame is never mutated.

        Returns:
            Cleaned DataFrame with columns ``ds`` (timezone-naive datetime64)
            and ``y`` (float64), plus any additional columns from the input.

        Raises:
            ValueError: If ``ds`` or ``y`` columns are absent, if ``ds``
                        cannot be coerced to datetime, or if ``y`` cannot be
                        coerced to float.

        Example::

            clean = pipeline.validate(raw_df)
            assert clean["ds"].dt.tz is None
            assert clean["y"].dtype == float
        """
        df = df.copy()

        # Step 1: required columns.
        self._assert_required_columns(df)

        # Step 2: parse ds → datetime64, strip tz.
        try:
            df["ds"] = pd.to_datetime(df["ds"])
        except Exception as exc:
            raise ValueError(
                f"Column 'ds' could not be coerced to datetime: {exc}"
            ) from exc

        if df["ds"].dt.tz is not None:
            df["ds"] = df["ds"].dt.tz_localize(None)
            logger.debug("validate | stripped timezone from 'ds'.")

        # Step 3: cast y to float64.
        try:
            df["y"] = pd.to_numeric(df["y"], errors="raise").astype("float64")
        except Exception as exc:
            raise ValueError(
                f"Column 'y' could not be coerced to float64: {exc}"
            ) from exc

        # Step 4: drop rows with missing ds.
        pre_len = len(df)
        df = df.dropna(subset=["ds"])
        dropped = pre_len - len(df)
        if dropped:
            logger.warning("validate | dropped %d rows with NaT in 'ds'.", dropped)

        if df.empty:
            logger.warning("validate | DataFrame is empty after dropping NaT ds rows.")
            return df.reset_index(drop=True)

        # Step 5: impute missing y.
        df = self._impute_y(df)

        # Step 6: winsorise y.
        df = self._winsorise_y(df)

        # Step 7 & 8: sort by ds ascending, reset index.
        df = df.sort_values("ds", ascending=True).reset_index(drop=True)

        logger.debug(
            "validate | output shape=%s | ds_tz=%s | y_dtype=%s",
            df.shape, df["ds"].dt.tz, df["y"].dtype,
        )

        return df

    def feature_engineering(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add calendar-based feature columns to a validated DataFrame.

        Derived features:
        - ``day_of_week`` — integer 0 (Monday) to 6 (Sunday).
        - ``month``       — integer 1 (January) to 12 (December).
        - ``is_weekend``  — boolean, True for Saturday (5) and Sunday (6).

        Args:
            df: A DataFrame that has already been through :meth:`validate`.
                Must contain a ``ds`` column of timezone-naive ``datetime64``.
                The original DataFrame is never mutated.

        Returns:
            DataFrame with three additional columns: ``day_of_week`` (int8),
            ``month`` (int8), ``is_weekend`` (bool).

        Raises:
            ValueError: If ``ds`` column is absent.

        Example::

            enriched = pipeline.feature_engineering(clean_df)
            assert "day_of_week" in enriched.columns
            assert "is_weekend" in enriched.columns
        """
        df = df.copy()

        if "ds" not in df.columns:
            raise ValueError(
                "feature_engineering() requires a 'ds' column. "
                "Run validate() first."
            )

        df["day_of_week"] = df["ds"].dt.dayofweek.astype("int8")
        df["month"] = df["ds"].dt.month.astype("int8")
        df["is_weekend"] = df["day_of_week"].isin([5, 6])

        logger.debug(
            "feature_engineering | added day_of_week, month, is_weekend | "
            "shape=%s", df.shape,
        )

        return df

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Run the full preprocessing pipeline: validate then feature_engineer.

        This is the primary entry point for callers. It is equivalent to::

            pipeline.feature_engineering(pipeline.validate(df))

        Args:
            df: Raw input DataFrame with ``ds`` and ``y`` columns.

        Returns:
            Fully cleaned and feature-enriched DataFrame, sorted by ``ds``
            ascending with a reset integer index.

        Raises:
            ValueError: Propagates from :meth:`validate` on schema or
                        type errors.

        Example::

            ready_df = pipeline.prepare(raw_df)
            # Pass ready_df directly to a forecasting model.
        """
        validated = self.validate(df)
        if validated.empty:
            logger.warning("prepare | returning empty DataFrame after validation.")
            return validated
        return self.feature_engineering(validated)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_required_columns(df: pd.DataFrame) -> None:
        """
        Raise ``ValueError`` if ``ds`` or ``y`` are not present in ``df``.

        Args:
            df: DataFrame to inspect.

        Raises:
            ValueError: Lists all missing required column names.
        """
        missing = [col for col in ("ds", "y") if col not in df.columns]
        if missing:
            raise ValueError(
                f"DataFrame is missing required columns: {missing}. "
                f"Present columns: {list(df.columns)}."
            )

    @staticmethod
    def _impute_y(df: pd.DataFrame) -> pd.DataFrame:
        """
        Impute missing values in the ``y`` column.

        Strategy:
        - Linear interpolation fills all interior NaN runs (both isolated and
          consecutive), preserving proportional distance between neighbours.
        - ``ffill`` / ``bfill`` handle leading or trailing NaN runs that
          interpolation cannot anchor.

        Args:
            df: DataFrame with ``y`` column (float64). Not mutated.

        Returns:
            DataFrame with ``y`` NaNs filled. Shape unchanged.
        """
        nan_count = df["y"].isna().sum()
        if nan_count == 0:
            return df

        logger.debug("_impute_y | filling %d NaN values in 'y'.", nan_count)

        df = df.copy()
        # Linear interpolation handles interior gaps (single and consecutive).
        df["y"] = df["y"].interpolate(method="linear", limit_direction="both")
        # Forward / backward fill catches any remaining boundary NaNs.
        df["y"] = df["y"].ffill().bfill()

        remaining = df["y"].isna().sum()
        if remaining:
            logger.warning(
                "_impute_y | %d NaN values remain after imputation "
                "(possibly all-NaN column).", remaining,
            )

        return df

    @staticmethod
    def _winsorise_y(df: pd.DataFrame) -> pd.DataFrame:
        """
        Cap ``y`` values at the 1st and 99th percentiles (Winsorization).

        Values below the 1st percentile are replaced by the 1st percentile;
        values above the 99th percentile are replaced by the 99th percentile.
        Values already within the bounds are unchanged.

        Skipped for DataFrames with fewer than 2 non-NaN ``y`` values, since
        percentiles are undefined for a single point.

        Args:
            df: DataFrame with ``y`` column (float64). Not mutated.

        Returns:
            DataFrame with ``y`` outliers capped. Shape unchanged.
        """
        valid_y = df["y"].dropna()
        if len(valid_y) < 2:
            logger.debug("_winsorise_y | fewer than 2 valid y values; skipping.")
            return df

        p1 = float(valid_y.quantile(_LOWER_PCT / 100.0))
        p99 = float(valid_y.quantile(_UPPER_PCT / 100.0))

        df = df.copy()
        clipped = df["y"].clip(lower=p1, upper=p99)
        n_clipped = int((df["y"] != clipped).sum())
        df["y"] = clipped

        if n_clipped:
            logger.debug(
                "_winsorise_y | capped %d values to [%.4f, %.4f].",
                n_clipped, p1, p99,
            )

        return df

    def __repr__(self) -> str:
        return "ForecastDataPipeline()"