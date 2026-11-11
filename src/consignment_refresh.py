"""Fetch and recalculate one consignment from Snowflake for Consignment Explorer."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
import sys

from src.daily_refresh import (
    DEFAULT_LOCK_PATH,
    DEFAULT_LOG_DIRECTORY,
    OUTPUTS,
    _refresh_environment,
    _run_lock,
    _run_module,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIRECTORY = OUTPUTS / "consignment_cache"


def run_consignment_refresh(
    *,
    invoice_number: str,
    site: str,
    despatch_date: str,
    cache_directory: Path = DEFAULT_CACHE_DIRECTORY,
    lock_path: Path = DEFAULT_LOCK_PATH,
    log_directory: Path = DEFAULT_LOG_DIRECTORY,
) -> Path:
    """Run the existing Stage 1-3 logic against one filtered Snowflake consignment."""
    invoice = str(invoice_number or "").strip()
    site_key = str(site or "").strip().upper()
    if not invoice:
        raise ValueError("A Shippit consignment number is required.")
    if site_key not in {"MOOREBANK", "TRUGANINA"}:
        raise ValueError("Site must be MOOREBANK or TRUGANINA.")
    dispatch_day = date.fromisoformat(str(despatch_date)[:10])

    safe_invoice = re.sub(r"[^A-Za-z0-9_.-]+", "_", invoice).strip("._") or "consignment"
    target_directory = cache_directory / f"{site_key.lower()}_{safe_invoice}"
    target_directory.mkdir(parents=True, exist_ok=True)
    filter_path = target_directory / "invoice_filter.csv"
    with filter_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["invoice_number"])
        writer.writeheader()
        writer.writerow({"invoice_number": invoice})

    start_text = dispatch_day.isoformat()
    environment = _refresh_environment(start_text, start_text)
    environment["LM_OUTPUT_DIR"] = str(target_directory)
    environment["LM_INVOICE_FILTER_PATH"] = str(filter_path)
    environment["PYTHONUNBUFFERED"] = "1"

    site_slug = site_key.lower()
    stage1_assignments = target_directory / f"stage1_{site_slug}_parcel_assignments_full.csv"
    stage1_lines = target_directory / f"stage1_{site_slug}_parcel_assignment_lines.csv"
    stage2_physicals = target_directory / f"stage2_{site_slug}_parcel_physicals_full.csv"
    stage3_estimates = target_directory / f"stage3_{site_slug}_cost_estimates_full.csv"
    stage_modules = {
        "MOOREBANK": (
            "src.stage1_moorebank_parcel_assigner",
            "src.stage2_moorebank_parcel_physical_estimator",
            "src.stage3_moorebank_cost_estimator",
        ),
        "TRUGANINA": (
            "src.stage1_truganina_parcel_assigner",
            "src.stage2_truganina_parcel_physical_estimator",
            "src.stage3_truganina_cost_estimator",
        ),
    }
    stage1_module, stage2_module, stage3_module = stage_modules[site_key]
    stage2_environment = environment.copy()
    stage2_environment.update(
        {
            "LM_STAGE1_INPUT_PATH": str(stage1_assignments),
            "LM_STAGE1_PARCEL_CONTENTS_PATH": str(stage1_lines),
            "LM_STAGE2_OUTPUT_PATH": str(stage2_physicals),
        }
    )
    stage3_environment = environment.copy()
    stage3_environment.update(
        {
            "LM_STAGE2_INPUT_PATH": str(stage2_physicals),
            "LM_STAGE3_OUTPUT_PATH": str(stage3_estimates),
        }
    )
    steps = [
        (stage1_module, environment),
        (stage2_module, stage2_environment),
        (stage3_module, stage3_environment),
    ]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"consignment_refresh_{site_key.lower()}_{safe_invoice}_{run_id}.log"
    with _run_lock(lock_path, f"consignment-{site_key}-{safe_invoice}-{run_id}"):
        for module, step_environment in steps:
            _run_module(module, (), step_environment, log_path)

    expected_output = stage3_estimates
    if not expected_output.exists():
        raise FileNotFoundError(f"The targeted estimate was not written: {expected_output}")
    print(f"CONSIGNMENT_STAGE3_OUTPUT={expected_output}")
    print(f"CONSIGNMENT_REFRESH_LOG={log_path}")
    return expected_output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice-number", required=True)
    parser.add_argument("--site", required=True, choices=["MOOREBANK", "TRUGANINA"])
    parser.add_argument("--despatch-date", required=True, help="Dispatch date in YYYY-MM-DD format")
    parser.add_argument("--cache-directory", type=Path, default=DEFAULT_CACHE_DIRECTORY)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--log-directory", type=Path, default=DEFAULT_LOG_DIRECTORY)
    args = parser.parse_args()
    try:
        run_consignment_refresh(
            invoice_number=args.invoice_number,
            site=args.site,
            despatch_date=args.despatch_date,
            cache_directory=args.cache_directory,
            lock_path=args.lock_path,
            log_directory=args.log_directory,
        )
    except Exception as error:
        print(f"Consignment refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
