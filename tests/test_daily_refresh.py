from datetime import date
import json
import os

from src.daily_refresh import (
    PATH_OVERRIDE_VARIABLES,
    _command_plan,
    _date_window,
    _refresh_environment,
    _read_status,
    _write_status,
)
from src.reconciliation_store import discover_persisted_coverage_paths


def test_daily_refresh_uses_inclusive_fourteen_day_window() -> None:
    assert _date_window(date(2026, 9, 1), 14) == ("2026-08-19", "2026-09-01")


def test_daily_refresh_persists_history_after_stage_three() -> None:
    plan = _command_plan("2026-08-19", "2026-09-01")

    assert [module for module, _ in plan[:6]] == [
        "src.stage1_moorebank_parcel_assigner",
        "src.stage1_truganina_parcel_assigner",
        "src.stage2_moorebank_parcel_physical_estimator",
        "src.stage2_truganina_parcel_physical_estimator",
        "src.stage3_moorebank_cost_estimator",
        "src.stage3_truganina_cost_estimator",
    ]
    assert plan[-1] == ("src.reconciliation_store", ("--dataset-name", "live"))
    assert all(arguments != ("--mode", "combine-discovered", "--dataset-name", "all_carrier_history") for _, arguments in plan)


def test_daily_refresh_can_optionally_rebuild_combined_history() -> None:
    plan = _command_plan(
        "2026-08-19",
        "2026-09-01",
        rebuild_history=True,
    )

    assert plan[-2] == ("src.reconciliation_store", ("--dataset-name", "live"))
    assert plan[-1] == (
        "src.reconciliation_store",
        ("--mode", "combine-discovered", "--dataset-name", "all_carrier_history"),
    )


def test_daily_refresh_can_include_invoice_reconciliation() -> None:
    plan = _command_plan(
        "2026-08-19",
        "2026-09-01",
        with_invoice_reconciliation=True,
    )

    modules = [module for module, _ in plan]
    assert modules.index("src.carrier_invoice_cp_ingest") < modules.index(
        "src.carrier_invoice_cp_reconciliation"
    )
    assert modules.index("src.carrier_invoice_australia_post_ingest") < modules.index(
        "src.carrier_invoice_australia_post_reconciliation"
    )
    assert plan[-1] == (
        "src.reconciliation_store",
        ("--mode", "refresh-invoices", "--dataset-name", "live"),
    )


def test_invoice_refresh_can_use_a_separate_ap_date_window() -> None:
    plan = _command_plan(
        "2026-08-25",
        "2026-09-07",
        with_invoice_reconciliation=True,
        invoice_start_date="2026-07-01",
        invoice_end_date="2026-07-31",
    )

    assert (
        "src.carrier_invoice_australia_post_ingest",
        ("--start-date", "2026-07-01", "--end-date", "2026-07-31"),
    ) in plan


def test_daily_refresh_forces_live_output_paths(monkeypatch) -> None:
    monkeypatch.setenv("LM_OUTPUT_DIR", "outputs/carrier_invoice_snapshots/old_run")
    monkeypatch.setenv("LM_STAGE3_OUTPUT_PATH", "outputs/carrier_invoice_snapshots/old_run/stage3.csv")

    environment = _refresh_environment("2026-08-19", "2026-09-01")

    assert environment["LM_OUTPUT_DIR"].endswith("outputs")
    assert environment["LM_EXTRACTION_START_DATE"] == "2026-08-19"
    assert environment["LM_EXTRACTION_END_DATE"] == "2026-09-01"
    assert all(variable not in environment for variable in PATH_OVERRIDE_VARIABLES if variable != "LM_OUTPUT_DIR")


def test_status_write_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "refresh_status.json"
    original_replace = type(path).replace
    attempts = {"count": 0}

    def flaky_replace(self, target):
        if self.name.startswith(".refresh_status.json") and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError("temporarily locked")
        return original_replace(self, target)

    monkeypatch.setattr(type(path), "replace", flaky_replace)
    _write_status(path, {"status": "succeeded"})

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "succeeded"
    assert attempts["count"] == 1


def test_status_read_uses_newer_fallback_when_main_file_is_stale(tmp_path) -> None:
    path = tmp_path / "refresh_status.json"
    fallback = tmp_path / "refresh_status.fallback.json"
    path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    fallback.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
    os.utime(path, (100, 100))
    os.utime(fallback, (200, 200))

    assert _read_status(path)["status"] == "succeeded"


def test_history_discovery_includes_live_combined_and_archived_stores() -> None:
    class FakePath:
        def __init__(self, value, *, exists=False, glob_results=(), rglob_results=()):
            self.value = value
            self._exists = exists
            self._glob_results = tuple(glob_results)
            self._rglob_results = tuple(rglob_results)
            self.children = {}

        def __truediv__(self, child):
            return self.children[str(child)]

        @property
        def name(self):
            return self.value.rstrip("/").split("/")[-1]

        def exists(self):
            return self._exists

        def glob(self, pattern):
            assert pattern == "*/consignment_invoice_coverage.csv"
            return self._glob_results

        def rglob(self, pattern):
            assert pattern == "consignment_invoice_coverage.csv"
            return self._rglob_results

        def __str__(self):
            return self.value

        def __hash__(self):
            return hash(self.value)

        def __eq__(self, other):
            return isinstance(other, FakePath) and self.value == other.value

    live = FakePath("root/reconciliation_store/live/consignment_invoice_coverage.csv")
    combined = FakePath("root/reconciliation_store/all_carrier_history/consignment_invoice_coverage.csv")
    archived = FakePath(
        "root/carrier_invoice_snapshots/snapshot_a/reconciliation_store/a/consignment_invoice_coverage.csv"
    )
    store_root = FakePath(
        "root/reconciliation_store",
        exists=True,
        glob_results=(live, combined),
    )
    snapshots_root = FakePath(
        "root/carrier_invoice_snapshots",
        exists=True,
        rglob_results=(archived,),
    )
    root = FakePath("root")
    root.children = {
        "reconciliation_store": store_root,
        "carrier_invoice_snapshots": snapshots_root,
    }

    discovered = discover_persisted_coverage_paths(root)

    assert discovered == sorted((live, combined, archived), key=lambda path: str(path).lower())
