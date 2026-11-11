from __future__ import annotations

import os
from datetime import date, timedelta


DEFAULT_EXTRACTION_LOOKBACK_DAYS = 6


def _configured_start_date() -> str:
    value = os.environ.get("LM_EXTRACTION_START_DATE", "").strip()
    if not value:
        return (date.today() - timedelta(days=DEFAULT_EXTRACTION_LOOKBACK_DAYS)).isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("LM_EXTRACTION_START_DATE must use YYYY-MM-DD format.") from error


def _configured_end_date() -> str:
    value = os.environ.get("LM_EXTRACTION_END_DATE", "").strip()
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("LM_EXTRACTION_END_DATE must use YYYY-MM-DD format.") from error


EXTRACTION_START_DATE = _configured_start_date()
EXTRACTION_END_DATE = _configured_end_date()

if date.fromisoformat(EXTRACTION_START_DATE) > date.fromisoformat(EXTRACTION_END_DATE):
    raise ValueError("LM_EXTRACTION_START_DATE must be on or before LM_EXTRACTION_END_DATE.")
