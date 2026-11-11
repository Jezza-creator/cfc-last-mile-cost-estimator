"""Refresh the accounting-week to finance-period reference from the date dimension."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from src.db import get_session


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = ROOT / "outputs" / "finance_period_week_map.csv"
DATE_DIMENSION = "KSF_SOPHIA_DATA_INTELLIGENCE_HUB_PROD.COMMON_DIMENSIONS.DIM_DATE"
FIELDS = (
    "accounting_year",
    "accounting_week_number_in_year",
    "accounting_period_number",
)


def refresh_finance_calendar(output_path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    """Write the authoritative fiscal year/week/period mapping used in reports."""
    session = get_session()
    try:
        rows = session.sql(
            f"""
            SELECT DISTINCT
                ACCOUNTING_YEAR AS ACCOUNTING_YEAR,
                ACCOUNTING_WEEK_NUMBER_IN_YEAR AS ACCOUNTING_WEEK_NUMBER_IN_YEAR,
                ACCOUNTING_PERIOD_NUMBER AS ACCOUNTING_PERIOD_NUMBER
            FROM {DATE_DIMENSION}
            WHERE ACCOUNTING_YEAR IS NOT NULL
              AND ACCOUNTING_WEEK_NUMBER_IN_YEAR IS NOT NULL
              AND ACCOUNTING_PERIOD_NUMBER IS NOT NULL
            ORDER BY ACCOUNTING_YEAR, ACCOUNTING_WEEK_NUMBER_IN_YEAR
            """
        ).collect()
    finally:
        session.close()

    mappings: dict[tuple[str, str], str] = {}
    for row in rows:
        year = str(row["ACCOUNTING_YEAR"] or "").strip()
        week = str(row["ACCOUNTING_WEEK_NUMBER_IN_YEAR"] or "").strip()
        period = str(row["ACCOUNTING_PERIOD_NUMBER"] or "").strip()
        if not year or not week or not period:
            continue
        key = (year, week)
        prior_period = mappings.get(key)
        if prior_period and prior_period != period:
            raise ValueError(
                f"Accounting week {year} W{week} maps to multiple Finance periods."
            )
        mappings[key] = period
    if not mappings:
        raise ValueError("The date dimension returned no accounting week/period mappings.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with temporary_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for (year, week), period in sorted(
                mappings.items(), key=lambda item: (int(item[0][0]), int(item[0][1]))
            ):
                writer.writerow(
                    {
                        "accounting_year": year,
                        "accounting_week_number_in_year": week,
                        "accounting_period_number": period,
                    }
                )
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(f"Wrote {len(mappings):,} accounting week/period mappings to {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    refresh_finance_calendar(args.output_path)


if __name__ == "__main__":
    main()
