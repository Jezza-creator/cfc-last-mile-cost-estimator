"""Check carrier invoice sources and run a carrier-specific reconciliation refresh."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from src.daily_refresh import (
    DEFAULT_LOCK_PATH,
    DEFAULT_LOG_DIRECTORY,
    OUTPUTS,
    _read_status,
    _refresh_environment,
    _run_lock,
    _run_module,
    _write_status,
)
from src.db import get_session


ROOT = Path(__file__).resolve().parents[1]
SOURCE_STATE_PATH = OUTPUTS / "invoice_source_state.json"
REFRESH_STATUS_PATH = OUTPUTS / "invoice_refresh_status.json"
AP_SOURCE_TABLE = "KSFPA.ONLINE_UGAM_PVT.OF_INV_AUSPOST"
AP_CONTROL_PATH = OUTPUTS / "carrier_invoices" / "australia_post_invoice_control_summary.csv"

CARRIER_LABELS = {
    "australia_post": "Australia Post",
    "couriers_please": "Couriers Please",
    "gopeople": "GoPeople",
    "allied": "Allied Express",
}

EXCEL_SOURCE_CONFIG = {
    "couriers_please": {
        "input_directory": ROOT / "data" / "inputs" / "carrier_invoices" / "couriers_please",
        "control_path": OUTPUTS / "carrier_invoices" / "couriers_please_invoice_control_summary.csv",
    },
    "gopeople": {
        "input_directory": ROOT / "data" / "inputs" / "carrier_invoices" / "GoPeople",
        "control_path": OUTPUTS / "carrier_invoices" / "gopeople_invoice_control_summary.csv",
    },
    "allied": {
        "input_directory": ROOT / "data" / "inputs" / "carrier_invoices" / "Allied",
        "control_path": OUTPUTS / "carrier_invoices" / "allied_invoice_control_summary.csv",
    },
}

AP_LATEST_QUERY = f"""
WITH normalised AS (
    SELECT
        TRY_TO_DATE(TO_VARCHAR(SERVICE_DATE), 'YYYYMMDD') AS service_date,
        TRY_TO_DATE(TO_VARCHAR(BILLING_DATE), 'YYYYMMDD') AS billing_date
    FROM {AP_SOURCE_TABLE}
)
SELECT
    MAX(IFF(service_date <= CURRENT_DATE(), service_date, NULL)) AS latest_valid_service_date,
    MAX(billing_date) AS latest_billing_date,
    COUNT(*) AS source_rows,
    COUNT_IF(service_date > CURRENT_DATE()) AS future_dated_rows,
    COUNT_IF(service_date IS NULL) AS invalid_service_date_rows
FROM normalised
"""


def check_invoice_availability(root: Path = ROOT) -> list[dict[str, object]]:
    """Return a lightweight availability status for each carrier invoice source."""
    statuses = [_check_ap_availability(root)]
    statuses.extend(
        _check_excel_availability(carrier, config, root)
        for carrier, config in EXCEL_SOURCE_CONFIG.items()
    )
    return statuses


def _check_excel_availability(
    carrier: str,
    config: dict[str, Path],
    root: Path = ROOT,
) -> dict[str, object]:
    input_directory = Path(config["input_directory"])
    control_path = Path(config["control_path"])
    if not input_directory.is_absolute():
        input_directory = root / input_directory
    if not control_path.is_absolute():
        control_path = root / control_path

    files = sorted(input_directory.glob("*.xlsx")) if input_directory.exists() else []
    processed_mtime = control_path.stat().st_mtime if control_path.exists() else None
    changed_files = [
        path
        for path in files
        if processed_mtime is None or path.stat().st_mtime > processed_mtime
    ]
    latest_file = max(files, key=lambda path: path.stat().st_mtime) if files else None
    new_available = bool(changed_files)
    if not files:
        status = "No source workbook found"
        detail = f"Expected .xlsx files in {input_directory}."
    elif new_available:
        status = "New or changed invoice workbook available"
        detail = (
            f"{len(files):,} workbook(s) found; {len(changed_files):,} new or changed "
            f"since the last processed control summary."
        )
    else:
        status = "No new invoice workbook detected"
        detail = f"{len(files):,} workbook(s) found; all are older than the processed control summary."

    return {
        "carrier_key": carrier,
        "carrier": CARRIER_LABELS[carrier],
        "source_type": "Excel workbooks",
        "status": status,
        "new_available": new_available,
        "can_refresh": bool(files),
        "latest_source_date": _format_timestamp(latest_file.stat().st_mtime) if latest_file else "",
        "latest_billing_date": "",
        "last_processed": _format_timestamp(processed_mtime) if processed_mtime else "Never",
        "detail": detail,
        "refresh_start_date": "",
        "refresh_end_date": "",
    }


def _changed_excel_source_files(carrier: str, root: Path = ROOT) -> tuple[str, ...]:
    """Return workbook names newer than the carrier's processed control file."""
    config = EXCEL_SOURCE_CONFIG[carrier]
    input_directory = Path(config["input_directory"])
    control_path = Path(config["control_path"])
    if not input_directory.is_absolute():
        input_directory = root / input_directory
    if not control_path.is_absolute():
        control_path = root / control_path
    processed_mtime = control_path.stat().st_mtime if control_path.exists() else None
    files = sorted(input_directory.glob("*.xlsx")) if input_directory.exists() else []
    return tuple(
        path.name
        for path in files
        if processed_mtime is None or path.stat().st_mtime > processed_mtime
    )


def _check_ap_availability(root: Path = ROOT) -> dict[str, object]:
    try:
        row = _fetch_ap_latest_metadata()
    except Exception as error:
        return {
            "carrier_key": "australia_post",
            "carrier": CARRIER_LABELS["australia_post"],
            "source_type": "Snowflake",
            "status": "Availability check failed",
            "new_available": False,
            "can_refresh": False,
            "latest_source_date": "",
            "latest_billing_date": "",
            "last_processed": "Unknown",
            "detail": f"Could not query {AP_SOURCE_TABLE}: {error}",
            "refresh_start_date": "",
            "refresh_end_date": "",
        }

    latest_service = _as_date(row.get("LATEST_VALID_SERVICE_DATE"))
    latest_billing = _as_date(row.get("LATEST_BILLING_DATE"))
    source_rows = _as_int(row.get("SOURCE_ROWS"))
    future_rows = _as_int(row.get("FUTURE_DATED_ROWS"))
    invalid_rows = _as_int(row.get("INVALID_SERVICE_DATE_ROWS"))
    processed_service = _latest_ap_processed_service_date(root)
    state = _read_json((root / SOURCE_STATE_PATH) if not SOURCE_STATE_PATH.is_absolute() else SOURCE_STATE_PATH)
    previous_source = state.get("australia_post", {}) if isinstance(state, dict) else {}
    previous_billing = _as_date(previous_source.get("latest_billing_date"))
    new_by_service = latest_service is not None and (
        processed_service is None or latest_service > processed_service
    )
    new_by_billing = latest_billing is not None and previous_billing is not None and latest_billing > previous_billing
    new_available = bool(source_rows and (new_by_service or new_by_billing))
    if not source_rows:
        status = "No AP invoice rows found"
    elif new_available:
        status = "New AP invoice information available"
    else:
        status = "No new AP invoice information detected"

    end_date = latest_service or date.today()
    start_date = end_date - timedelta(days=13)
    detail = (
        f"{source_rows:,} source rows; latest valid service date "
        f"{_format_date(latest_service) or 'not available'}; latest billing date "
        f"{_format_date(latest_billing) or 'not available'}."
    )
    if processed_service:
        detail += f" Last processed service window ended {_format_date(processed_service)}."
    if future_rows or invalid_rows:
        detail += f" Excluded from the check: {future_rows:,} future-dated and {invalid_rows:,} invalid service-date rows."

    return {
        "carrier_key": "australia_post",
        "carrier": CARRIER_LABELS["australia_post"],
        "source_type": "Snowflake",
        "status": status,
        "new_available": new_available,
        "can_refresh": bool(source_rows),
        "latest_source_date": _format_date(latest_service),
        "latest_billing_date": _format_date(latest_billing),
        "last_processed": _format_date(processed_service) if processed_service else "Never recorded",
        "detail": detail,
        "refresh_start_date": start_date.isoformat(),
        "refresh_end_date": end_date.isoformat(),
    }


def _fetch_ap_latest_metadata() -> dict[str, object]:
    session = get_session()
    try:
        rows = session.sql(AP_LATEST_QUERY).collect()
        return rows[0].as_dict() if rows else {}
    finally:
        session.close()


def _latest_ap_processed_service_date(root: Path = ROOT) -> date | None:
    path = root / AP_CONTROL_PATH if not AP_CONTROL_PATH.is_absolute() else AP_CONTROL_PATH
    if not path.exists():
        return None
    dates: list[date] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            value = _as_date(row.get("requested_end_date"))
            if value:
                dates.append(value)
    return max(dates) if dates else None


def invoice_refresh_plan(
    carrier: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    incremental: bool = True,
    source_files: tuple[str, ...] = (),
) -> list[tuple[str, tuple[str, ...]]]:
    """Return carrier-specific ingest, reconcile, and live-store steps."""
    if carrier not in CARRIER_LABELS:
        raise ValueError(f"Unsupported invoice carrier: {carrier}")

    history_stage3 = OUTPUTS / "reconciliation_store" / "live"
    history_stage3_arguments = (
        "--moorebank-stage3",
        str(history_stage3 / "stage3_moorebank_invoice_history.csv"),
        "--truganina-stage3",
        str(history_stage3 / "stage3_truganina_invoice_history.csv"),
    )
    if carrier == "australia_post":
        end = date.fromisoformat(end_date) if end_date else date.today()
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=13)
        if start > end:
            raise ValueError("Invoice start date must be on or before invoice end date.")
        steps = [
            (
                "src.carrier_invoice_australia_post_ingest",
                ("--start-date", start.isoformat(), "--end-date", end.isoformat()),
            ),
            (
                "src.reconciliation_store",
                ("--mode", "export-stage3-history", "--dataset-name", "live"),
            ),
            ("src.carrier_invoice_australia_post_reconciliation", history_stage3_arguments),
        ]
    else:
        ingest_modules = {
            "couriers_please": "src.carrier_invoice_cp_ingest",
            "gopeople": "src.carrier_invoice_gopeople_ingest",
            "allied": "src.carrier_invoice_allied_ingest",
        }
        reconciliation_modules = {
            "couriers_please": "src.carrier_invoice_cp_reconciliation",
            "gopeople": "src.carrier_invoice_gopeople_reconciliation",
            "allied": "src.carrier_invoice_allied_reconciliation",
        }
        steps = [
            (ingest_modules[carrier], ()),
            (
                "src.reconciliation_store",
                ("--mode", "export-stage3-history", "--dataset-name", "live"),
            ),
            (reconciliation_modules[carrier], history_stage3_arguments),
        ]

    store_arguments = ["--mode", "refresh-invoices", "--dataset-name", "live"]
    if incremental:
        store_arguments.extend(["--incremental-carrier", carrier])
        if carrier == "australia_post":
            store_arguments.extend(
                [
                    "--invoice-start-date",
                    (date.fromisoformat(start_date) if start_date else date.today() - timedelta(days=13)).isoformat(),
                    "--invoice-end-date",
                    (date.fromisoformat(end_date) if end_date else date.today()).isoformat(),
                ]
            )
        for source_file in source_files:
            store_arguments.extend(["--invoice-source-file", source_file])
    steps.append(("src.reconciliation_store", tuple(store_arguments)))
    return steps


def run_invoice_refresh(
    *,
    carrier: str,
    start_date: str | None = None,
    end_date: str | None = None,
    status_path: Path = REFRESH_STATUS_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    log_directory: Path = DEFAULT_LOG_DIRECTORY,
    dry_run: bool = False,
    full_rematch: bool = False,
) -> Path | None:
    source_files = (
        _changed_excel_source_files(carrier)
        if carrier in EXCEL_SOURCE_CONFIG and not full_rematch
        else ()
    )
    plan = invoice_refresh_plan(
        carrier,
        start_date,
        end_date,
        incremental=not full_rematch,
        source_files=source_files,
    )
    if dry_run:
        for module, arguments in plan:
            print(" ".join([sys.executable, "-m", module, *arguments]))
        return None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"invoice_refresh_{carrier}_{run_id}.log"
    previous_status = _read_status(status_path)
    status: dict[str, object] = {
        "status": "running",
        "carrier_key": carrier,
        "carrier": CARRIER_LABELS[carrier],
        "run_id": run_id,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "finished_at_utc": None,
        "completed_steps": 0,
        "total_steps": len(plan),
        "current_step": None,
        "log_file": str(log_path),
        "output_directory": str(OUTPUTS),
        "last_successful_refresh_utc": previous_status.get("last_successful_refresh_utc"),
        "error": None,
    }
    if start_date:
        status["invoice_start_date"] = start_date
    if end_date:
        status["invoice_end_date"] = end_date

    effective_start = start_date or (date.today() - timedelta(days=13)).isoformat()
    effective_end = end_date or date.today().isoformat()
    environment = _refresh_environment(effective_start, effective_end)
    lock_acquired = False
    try:
        with _run_lock(lock_path, f"invoice-{carrier}-{run_id}"):
            lock_acquired = True
            _write_status(status_path, status)
            for step_number, (module, arguments) in enumerate(plan, start=1):
                status["current_step"] = module + (f" {' '.join(arguments)}" if arguments else "")
                _write_status(status_path, status)
                _run_module(module, arguments, environment, log_path)
                status["completed_steps"] = step_number
                _write_status(status_path, status)

            source_snapshot_warning = _record_source_snapshot(carrier)
            finished_at = datetime.now(timezone.utc).isoformat()
            status.update(
                {
                    "status": "succeeded",
                    "finished_at_utc": finished_at,
                    "current_step": None,
                    "last_successful_refresh_utc": finished_at,
                    "source_snapshot_warning": source_snapshot_warning,
                }
            )
            _write_status(status_path, status)
    except Exception as error:
        if lock_acquired:
            status.update(
                {
                    "status": "failed",
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "error": str(error),
                }
            )
            _write_status(status_path, status)
        raise
    return log_path


def _record_source_snapshot(carrier: str) -> str:
    try:
        state = _read_json(SOURCE_STATE_PATH)
        if not isinstance(state, dict):
            state = {}
        if carrier == "australia_post":
            metadata = _fetch_ap_latest_metadata()
            state[carrier] = {
                "latest_service_date": _date_to_text(_as_date(metadata.get("LATEST_VALID_SERVICE_DATE"))),
                "latest_billing_date": _date_to_text(_as_date(metadata.get("LATEST_BILLING_DATE"))),
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        else:
            config = EXCEL_SOURCE_CONFIG[carrier]
            files = sorted(Path(config["input_directory"]).glob("*.xlsx"))
            state[carrier] = {
                "files": {path.name: _format_timestamp(path.stat().st_mtime) for path in files},
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        _write_json(SOURCE_STATE_PATH, state)
        return ""
    except Exception as error:  # Source snapshot metadata must not mask a successful reconciliation.
        return str(error)


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for format_string in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, format_string).date()
        except ValueError:
            continue
    return None


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _date_to_text(value: date | None) -> str:
    return value.isoformat() if value else ""


def _format_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carrier", choices=tuple(CARRIER_LABELS), required=True)
    parser.add_argument("--start-date", help="Inclusive AP SERVICE_DATE start in YYYY-MM-DD format.")
    parser.add_argument("--end-date", help="Inclusive AP SERVICE_DATE end in YYYY-MM-DD format.")
    parser.add_argument("--status-path", type=Path, default=REFRESH_STATUS_PATH)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--log-directory", type=Path, default=DEFAULT_LOG_DIRECTORY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--full-rematch",
        action="store_true",
        help="Rematch the complete persisted history instead of the affected invoice scope.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        log_path = run_invoice_refresh(
            carrier=args.carrier,
            start_date=args.start_date,
            end_date=args.end_date,
            status_path=args.status_path,
            lock_path=args.lock_path,
            log_directory=args.log_directory,
            dry_run=args.dry_run,
            full_rematch=args.full_rematch,
        )
    except Exception as error:
        print(f"Invoice refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if log_path is not None:
        print(f"{CARRIER_LABELS[args.carrier]} invoice refresh completed. Log: {log_path}")


if __name__ == "__main__":
    main()
