"""Run the daily operational Stage 1-3 refresh and persist the live rolling history."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
DEFAULT_STATUS_PATH = OUTPUTS / "refresh_status.json"
DEFAULT_LOCK_PATH = OUTPUTS / "refresh_live.lock"
DEFAULT_LOG_DIRECTORY = OUTPUTS / "refresh_logs"
STATUS_WRITE_RETRIES = 8
STATUS_WRITE_RETRY_DELAY_SECONDS = 0.25
PATH_OVERRIDE_VARIABLES = (
    "LM_OUTPUT_DIR",
    "LM_INVOICE_FILTER_PATH",
    "LM_STAGE1_INPUT_PATH",
    "LM_STAGE1_PARCEL_CONTENTS_PATH",
    "LM_STAGE2_INPUT_PATH",
    "LM_STAGE2_OUTPUT_PATH",
    "LM_STAGE3_OUTPUT_PATH",
)

STAGE_MODULES = (
    "src.stage1_moorebank_parcel_assigner",
    "src.stage1_truganina_parcel_assigner",
    "src.stage2_moorebank_parcel_physical_estimator",
    "src.stage2_truganina_parcel_physical_estimator",
    "src.stage3_moorebank_cost_estimator",
    "src.stage3_truganina_cost_estimator",
)
INVOICE_INGEST_MODULES = (
    ("src.carrier_invoice_cp_ingest", ()),
    ("src.carrier_invoice_gopeople_ingest", ()),
    ("src.carrier_invoice_allied_ingest", ()),
)
INVOICE_RECONCILIATION_MODULES = (
    ("src.carrier_invoice_cp_reconciliation", ()),
    ("src.carrier_invoice_gopeople_reconciliation", ()),
    ("src.carrier_invoice_allied_reconciliation", ()),
)


def _date_window(end_date: date | None, lookback_days: int) -> tuple[str, str]:
    if lookback_days < 1:
        raise ValueError("--lookback-days must be at least 1.")
    end = end_date or date.today()
    start = end - timedelta(days=lookback_days - 1)
    return start.isoformat(), end.isoformat()


def _command_plan(
    start_date: str,
    end_date: str,
    with_invoice_reconciliation: bool = False,
    rebuild_history: bool = False,
    invoice_start_date: str | None = None,
    invoice_end_date: str | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    plan = [(module, ()) for module in STAGE_MODULES]
    # Period assignments are a tiny DIM_DATE lookup and keep Finance grouping
    # reliable for both new shipment rows and retained legacy history.
    plan.append(("src.finance_calendar_refresh", ()))
    if with_invoice_reconciliation:
        plan.extend(INVOICE_INGEST_MODULES)
        plan.append(
            (
                "src.carrier_invoice_australia_post_ingest",
                (
                    "--start-date",
                    invoice_start_date or start_date,
                    "--end-date",
                    invoice_end_date or end_date,
                ),
            )
        )
        plan.extend(INVOICE_RECONCILIATION_MODULES)
        plan.append(("src.carrier_invoice_australia_post_reconciliation", ()))
    if with_invoice_reconciliation:
        # Invoice files may contain older weeks, so re-match the persisted
        # history after ingesting them. This is intentionally an explicit,
        # slower invoice-arrival path rather than part of the daily run.
        plan.append(
            (
                "src.reconciliation_store",
                ("--mode", "refresh-invoices", "--dataset-name", "live"),
            )
        )
    else:
        plan.append(("src.reconciliation_store", ("--dataset-name", "live")))
    if rebuild_history:
        plan.append(
            (
                "src.reconciliation_store",
                ("--mode", "combine-discovered", "--dataset-name", "all_carrier_history"),
            )
        )
    return plan


def _read_status(path: Path) -> dict[str, object]:
    candidates = [path, path.with_name(f"{path.stem}.fallback{path.suffix}")]
    existing = sorted(
        (candidate for candidate in candidates if candidate.exists()),
        key=lambda candidate: candidate.stat().st_mtime_ns,
        reverse=True,
    )
    for candidate in existing:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _write_status(path: Path, status: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(status, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    last_error: OSError | None = None

    try:
        temporary.write_text(payload, encoding="utf-8")
        for attempt in range(STATUS_WRITE_RETRIES):
            try:
                temporary.replace(path)
                return
            except PermissionError as error:
                last_error = error
                if attempt + 1 < STATUS_WRITE_RETRIES:
                    time.sleep(STATUS_WRITE_RETRY_DELAY_SECONDS)
    except OSError as error:
        last_error = error
    finally:
        temporary.unlink(missing_ok=True)

    fallback = path.with_name(f"{path.stem}.fallback{path.suffix}")
    fallback_temporary = fallback.with_name(f".{fallback.name}.{os.getpid()}.tmp")
    try:
        fallback_temporary.write_text(payload, encoding="utf-8")
        fallback_temporary.replace(fallback)
        print(
            f"Warning: could not replace refresh status file {path}; "
            f"wrote fallback status to {fallback}. Last error: {last_error}"
        )
    except OSError as fallback_error:
        fallback_temporary.unlink(missing_ok=True)
        print(
            f"Warning: could not write refresh status file {path} or fallback "
            f"{fallback}: {fallback_error}"
        )


@contextmanager
def _run_lock(path: Path, run_id: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        )
        if age > timedelta(hours=24):
            path.unlink()
        else:
            raise RuntimeError(
                f"A daily refresh appears to be running already: {path}. "
                "Check the refresh status and log before removing the lock."
            )
    handle = path.open("x", encoding="utf-8")
    try:
        handle.write(run_id)
        handle.flush()
        yield
    finally:
        handle.close()
        path.unlink(missing_ok=True)


def _run_module(
    module: str,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    log_path: Path,
) -> None:
    command = [sys.executable, "-m", module, *arguments]
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(command)}\n")
        log.flush()
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(
            f"{module} failed with exit code {result.returncode}. See {log_path}."
        )


def _refresh_environment(start_date: str, end_date: str) -> dict[str, str]:
    """Build a clean live-run environment, avoiding historical path leakage."""
    environment = os.environ.copy()
    for variable in PATH_OVERRIDE_VARIABLES:
        environment.pop(variable, None)
    environment["LM_OUTPUT_DIR"] = str(OUTPUTS)
    environment["LM_EXTRACTION_START_DATE"] = start_date
    environment["LM_EXTRACTION_END_DATE"] = end_date
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def run_refresh(
    *,
    end_date: date | None = None,
    lookback_days: int = 14,
    with_invoice_reconciliation: bool = False,
    rebuild_history: bool = False,
    invoice_start_date: date | None = None,
    invoice_end_date: date | None = None,
    status_path: Path = DEFAULT_STATUS_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    log_directory: Path = DEFAULT_LOG_DIRECTORY,
    dry_run: bool = False,
) -> Path | None:
    start_text, end_text = _date_window(end_date, lookback_days)
    invoice_start_text = invoice_start_date.isoformat() if invoice_start_date else start_text
    invoice_end_text = invoice_end_date.isoformat() if invoice_end_date else end_text
    if invoice_start_text > invoice_end_text:
        raise ValueError("Invoice start date must be on or before invoice end date.")
    plan = _command_plan(
        start_text,
        end_text,
        with_invoice_reconciliation,
        rebuild_history,
        invoice_start_text,
        invoice_end_text,
    )
    if dry_run:
        for module, arguments in plan:
            print(" ".join([sys.executable, "-m", module, *arguments]))
        return None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = datetime.now(timezone.utc).isoformat()
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"daily_refresh_{run_id}.log"
    previous_status = _read_status(status_path)
    status: dict[str, object] = {
        "status": "running",
        "run_id": run_id,
        "started_at_utc": started_at,
        "finished_at_utc": None,
        "extraction_start_date": start_text,
        "extraction_end_date": end_text,
        "lookback_days": lookback_days,
        "with_invoice_reconciliation": with_invoice_reconciliation,
        "rebuild_history": rebuild_history,
        "invoice_start_date": invoice_start_text if with_invoice_reconciliation else None,
        "invoice_end_date": invoice_end_text if with_invoice_reconciliation else None,
        "completed_steps": 0,
        "total_steps": len(plan),
        "current_step": None,
        "log_file": str(log_path),
        "output_directory": str(OUTPUTS),
        "last_successful_refresh_utc": previous_status.get(
            "last_successful_refresh_utc"
        ),
        "error": None,
    }

    environment = _refresh_environment(start_text, end_text)
    lock_acquired = False
    try:
        with _run_lock(lock_path, run_id):
            lock_acquired = True
            _write_status(status_path, status)
            for step_number, (module, arguments) in enumerate(plan, start=1):
                status["current_step"] = module + (
                    f" {' '.join(arguments)}" if arguments else ""
                )
                _write_status(status_path, status)
                _run_module(module, arguments, environment, log_path)
                status["completed_steps"] = step_number
                _write_status(status_path, status)
            finished_at = datetime.now(timezone.utc).isoformat()
            status.update(
                {
                    "status": "succeeded",
                    "finished_at_utc": finished_at,
                    "current_step": None,
                    "last_successful_refresh_utc": finished_at,
                }
            )
            _write_status(status_path, status)
    except Exception as error:
        if lock_acquired:
            status.update(
                {
                    "status": "failed",
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "current_step": status.get("current_step"),
                    "error": str(error),
                }
            )
            _write_status(status_path, status)
        raise
    return log_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="Inclusive extraction window ending on --end-date (default: 14).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="Inclusive extraction end date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--with-invoice-reconciliation",
        action="store_true",
        help=(
            "Ingest and reconcile all available carrier invoice feeds, then refresh "
            "the persisted invoice history. This is intentionally slower than the daily path."
        ),
    )
    parser.add_argument(
        "--rebuild-history",
        action="store_true",
        help="Also rebuild the combined all-carrier history after the daily run.",
    )
    parser.add_argument(
        "--invoice-start-date",
        type=date.fromisoformat,
        default=None,
        help="Optional inclusive Australia Post invoice SERVICE_DATE start; defaults to the Stage 1-3 window.",
    )
    parser.add_argument(
        "--invoice-end-date",
        type=date.fromisoformat,
        default=None,
        help="Optional inclusive Australia Post invoice SERVICE_DATE end; defaults to the Stage 1-3 window.",
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=DEFAULT_STATUS_PATH,
        help="Path for the machine-readable refresh status JSON.",
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help="Path used to prevent overlapping refresh runs.",
    )
    parser.add_argument(
        "--log-directory",
        type=Path,
        default=DEFAULT_LOG_DIRECTORY,
        help="Directory for refresh logs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned commands without running Snowflake or writing outputs.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        log_path = run_refresh(
            end_date=args.end_date,
            lookback_days=args.lookback_days,
            with_invoice_reconciliation=args.with_invoice_reconciliation,
            rebuild_history=args.rebuild_history,
            invoice_start_date=args.invoice_start_date,
            invoice_end_date=args.invoice_end_date,
            status_path=args.status_path,
            lock_path=args.lock_path,
            log_directory=args.log_directory,
            dry_run=args.dry_run,
        )
    except Exception as error:
        print(f"Daily refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if log_path is not None:
        print(f"Daily refresh completed. Log: {log_path}")


if __name__ == "__main__":
    main()
