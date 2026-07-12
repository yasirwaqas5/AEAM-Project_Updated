"""
aeam/ingestion/schema_inference.py

Schema inference for structured uploads (Phase B1.4 — Dataset & Schema Registration).

The single genuinely-new capability B1.4 adds: read a tabular file (CSV/Excel)
into a DataFrame and infer a schema — per-column ``type``/``nullable``/``role``
plus which columns are monitored ``metric`` columns — matching the shape the
B1.1 blueprint declared for ``schemas.columns``::

    [{name, type, nullable, is_metric, role}]

Pure and stateless: no blob, registry, network, or Qdrant access. ``pandas`` is
already a core dependency (and ``openpyxl`` was added for Excel in B1.3), so no
new dependency is introduced. Text extraction (unstructured → text for RAG)
stays in ``extraction.py``; this module is its structured counterpart
(tabular → schema).
"""

from __future__ import annotations

import io
from typing import Any

# Canonical column types this module emits.
TYPE_INTEGER = "integer"
TYPE_FLOAT = "float"
TYPE_BOOLEAN = "boolean"
TYPE_DATETIME = "datetime"
TYPE_STRING = "string"

# Canonical column roles.
ROLE_METRIC = "metric"          # numeric measure — a monitorable KPI candidate
ROLE_TIMESTAMP = "timestamp"    # time axis
ROLE_IDENTIFIER = "identifier"  # id / key column
ROLE_DIMENSION = "dimension"    # categorical / descriptive


class SchemaInferenceError(Exception):
    """
    Raised when a structured file cannot be read or profiled. Carries a stable
    ``reason`` code plus a human ``detail`` (mirrors ExtractionError), so the
    job worker records a structured, greppable failure.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def _pandas():
    try:
        import pandas as pd  # noqa: PLC0415 (lazy by design)
        return pd
    except ImportError as exc:  # pragma: no cover - pandas is a core dependency
        raise SchemaInferenceError(
            "missing_dependency", "pandas is required to profile structured files."
        ) from exc


def _is_identifier_name(name: str) -> bool:
    """Conservative id-column heuristic: 'id' or '*_id' (not 'paid'/'valid')."""
    low = name.strip().lower()
    return low == "id" or low.endswith("_id")


def read_primary_table(
    data: bytes,
    category: str,
    object_name: str = "data",
) -> tuple[Any, dict[str, Any]]:
    """
    Read the primary table out of ``data`` as a pandas DataFrame.

    CSV yields its single table. Excel yields the first non-empty sheet (a
    workbook's remaining sheets are noted in ``detail`` and deferred to a later
    phase — one dataset per uploaded file in this slice). Unknown categories are
    sniffed CSV-then-Excel.

    Returns:
        ``(dataframe, detail)`` where ``detail`` carries format facts
        (e.g. ``{"format": "excel", "sheet_count": 3, "sheet_name": "Q3"}``).

    Raises:
        SchemaInferenceError: If the file cannot be parsed or has no data rows.
    """
    pd = _pandas()

    def _read_csv() -> Any:
        try:
            return pd.read_csv(io.BytesIO(data))
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(data), encoding="latin-1")

    def _read_excel_first() -> tuple[Any, dict[str, Any]]:
        try:
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
        except ImportError as exc:
            raise SchemaInferenceError(
                "missing_dependency",
                "openpyxl (for .xlsx) or xlrd (for .xls) is required to read Excel files.",
            ) from exc
        for name, sdf in sheets.items():
            if not sdf.empty:
                return sdf, {"format": "excel", "sheet_count": len(sheets), "sheet_name": name}
        raise SchemaInferenceError("empty_dataset", "Excel workbook has no non-empty sheet.")

    try:
        if category == "csv":
            df, detail = _read_csv(), {"format": "csv"}
        elif category == "excel":
            df, detail = _read_excel_first()
        else:
            # Unknown/derived category — best-effort sniff.
            try:
                df, detail = _read_csv(), {"format": "csv"}
            except Exception:
                df, detail = _read_excel_first()
    except SchemaInferenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - EmptyDataError, ParserError, ...
        raise SchemaInferenceError(
            "parse_error", f"Could not read structured file '{object_name}': {exc}"
        ) from exc

    if df is None or df.shape[1] == 0:
        raise SchemaInferenceError("empty_dataset", f"'{object_name}' has no columns.")
    if df.empty:
        raise SchemaInferenceError("empty_dataset", f"'{object_name}' has no data rows.")
    return df, detail


def infer_schema(df: Any, object_name: str = "data") -> dict[str, Any]:
    """
    Infer a registry-ready schema from a pandas DataFrame.

    Returns a dict shaped for direct storage in the ``schemas``/``datasets``
    registry rows::

        {
            "object_name":    str,
            "columns":        [{name, type, nullable, is_metric, role}, ...],
            "row_count":      int,
            "metric_columns": [str, ...],   # names where role == 'metric'
        }

    Column typing rules (from the pandas dtype):
      - bool         -> boolean, role dimension
      - integer/float-> integer/float; role metric, unless the name is id-like
                        ('id' / '*_id') in which case identifier
      - datetime     -> datetime, role timestamp
      - everything else -> string, role dimension

    Raises:
        SchemaInferenceError: If ``df`` has no columns.
    """
    pd = _pandas()
    if df is None or df.shape[1] == 0:
        raise SchemaInferenceError("empty_dataset", f"'{object_name}' has no columns.")

    columns: list[dict[str, Any]] = []
    metric_columns: list[str] = []

    for col in df.columns:
        name = str(col)
        series = df[col]
        nullable = bool(series.isna().any())

        if pd.api.types.is_bool_dtype(series):
            ctype, role = TYPE_BOOLEAN, ROLE_DIMENSION
        elif pd.api.types.is_datetime64_any_dtype(series):
            ctype, role = TYPE_DATETIME, ROLE_TIMESTAMP
        elif pd.api.types.is_integer_dtype(series):
            ctype = TYPE_INTEGER
            role = ROLE_IDENTIFIER if _is_identifier_name(name) else ROLE_METRIC
        elif pd.api.types.is_float_dtype(series):
            ctype = TYPE_FLOAT
            role = ROLE_IDENTIFIER if _is_identifier_name(name) else ROLE_METRIC
        else:
            ctype, role = TYPE_STRING, ROLE_DIMENSION

        is_metric = role == ROLE_METRIC
        if is_metric:
            metric_columns.append(name)

        columns.append({
            "name": name,
            "type": ctype,
            "nullable": nullable,
            "is_metric": is_metric,
            "role": role,
        })

    return {
        "object_name": object_name,
        "columns": columns,
        "row_count": int(len(df)),
        "metric_columns": metric_columns,
    }


def infer_dataset_schema(
    data: bytes,
    category: str,
    object_name: str = "data",
) -> dict[str, Any]:
    """
    Convenience: read ``data`` and infer its schema in one call.

    Returns the :func:`infer_schema` dict with an extra ``detail`` key carrying
    the format facts from :func:`read_primary_table` (e.g. sheet count).
    """
    df, detail = read_primary_table(data, category, object_name=object_name)
    result = infer_schema(df, object_name=object_name)
    result["detail"] = detail
    return result
