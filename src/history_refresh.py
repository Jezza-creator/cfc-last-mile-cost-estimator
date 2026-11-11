"""Rebuild the combined all-carrier history independently of the daily refresh."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

from .daily_refresh import (
    DEFAULT_LOCK_PATH,
    DEFAULT_LOG_DIRECTORY,
    OUTPUTS,
    _read_status,
    _run_lock,
    _run_module,
    _write_status,
)


DEFAULT_STATUS_PATH = OUTPUTS / "history_refresh_status.json"


def run_history_refresh(
    *,
    dataset_name: str = "all_carrier_history",
    output_directory: Path = OUTPUTS / "reconciliation_store",
    status_path: Path = DEFAULT_STATUS_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    log_directory: Path = DEFAULT_LOG_DIRECTORY,
    dry_run: bool = False,
) -> Path | None:
    arguments = (
        "--mode",
        "combine-discovered",
        "--dataset-name",
        dataset_name,
        "--output-directory",
        str(output_directory),
    )
    if dry_run:
        print(" ".join([sys.executable, "-m", "src.reconciliation_store", *arguments]))
        return None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = datetime.now(timezone.utc).isoformat()
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"history_refresh_{run_id}.log"
    previous_status = _read_status(status_path)
    status: dict[str, object] = {
        "status": "running",
        "run_id": run_id,
        "started_at_utc": started_at,
        "finished_at_utc": None,
        "completed_steps": 0,
        "total_steps": 1,
        "current_step": "all_carrier_history",
        "log_file": str(log_path),
        "output_directory": str(output_directory),
        "last_successful_refresh_utc": previous_status.get(
            "last_successful_refresh_utc"
        ),
        "error": None,
    }
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"

    try:
        with _run_lock(lock_path, f"history-{run_id}"):
            _write_status(status_path, status)
            _run_module(
                "src.reconciliation_store",
                arguments,
                environment,
                log_path,
            )
            finished_at = datetime.now(timezone.utc).isoformat()
            status.update(
                {
                    "status": "succeeded",
                    "finished_at_utc": finished_at,
                    "completed_steps": 1,
                    "current_step": None,
                    "last_successful_refresh_utc": finished_at,
                }
            )
            _write_status(status_path, status)
    except Exception as error:
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-name", default="all_carrier_history")
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=OUTPUTS / "reconciliation_store",
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=DEFAULT_STATUS_PATH,
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_LOCK_PATH,
    )
    parser.add_argument(
        "--log-directory",
        type=Path,
        default=DEFAULT_LOG_DIRECTORY,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned command without rebuilding history.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        log_path = run_history_refresh(
            dataset_name=args.dataset_name,
            output_directory=args.output_directory,
            status_path=args.status_path,
            lock_path=args.lock_path,
            log_directory=args.log_directory,
            dry_run=args.dry_run,
        )
    except Exception as error:
        print(f"History refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if log_path is not None:
        print(f"History refresh completed. Log: {log_path}")


if __name__ == "__main__":
    main()
