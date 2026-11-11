"""Helpers for applying an optional invoice list to Snowflake queries."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable


INVOICE_FILTER_ENV = "LM_INVOICE_FILTER_PATH"


def load_invoice_numbers(path: Path | None = None) -> list[str]:
    """Load invoice numbers from a CSV with an ``invoice_number`` column."""
    configured_path = path or _configured_path()
    if configured_path is None or not configured_path.exists():
        return []

    with configured_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "invoice_number" not in reader.fieldnames:
            raise ValueError(f"Invoice filter must contain an invoice_number column: {configured_path}")
        values = {str(row.get("invoice_number") or "").strip() for row in reader}
    return sorted(value for value in values if value)


def invoice_filter_clause(column: str, invoices: Iterable[str] | None = None) -> str:
    """Return an ``AND ... IN (...)`` predicate, or an empty string."""
    if invoices is None:
        configured_path = _configured_path()
        if configured_path is None:
            return ""
        if not configured_path.exists():
            raise FileNotFoundError(f"Invoice filter file does not exist: {configured_path}")
        values = sorted(set(load_invoice_numbers(configured_path)))
        if not values:
            return "AND 1 = 0"
    else:
        values = sorted({str(value or "").strip() for value in invoices if str(value or "").strip()})
    if not values:
        return ""
    escaped = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"AND {column} IN ({escaped})"


def _configured_path() -> Path | None:
    value = os.environ.get(INVOICE_FILTER_ENV, "").strip()
    return Path(value) if value else None
