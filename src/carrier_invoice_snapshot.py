"""Archive model and carrier-invoice outputs needed to reproduce a reconciliation."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


OUTPUTS = Path("outputs")
SNAPSHOT_ROOT = OUTPUTS / "carrier_invoice_snapshots"
SNAPSHOT_FILES = (
    OUTPUTS / "stage3_moorebank_cost_estimates_full.csv",
    OUTPUTS / "stage3_truganina_cost_estimates_full.csv",
    OUTPUTS / "carrier_invoices" / "couriers_please_charge_lines.csv",
    OUTPUTS / "carrier_invoices" / "couriers_please_invoice_control_summary.csv",
    OUTPUTS / "carrier_invoices" / "couriers_please_reconciliation.csv",
    OUTPUTS / "carrier_invoices" / "couriers_please_reconciliation_summary.csv",
)
OPTIONAL_SNAPSHOT_FILES = (
    OUTPUTS / "carrier_invoices" / "australia_post_charge_lines.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_consignments.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_invoice_control_summary.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_reconciliation.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_reconciliation_summary.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_dimension_diagnostics_summary.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_reconciliation_cost_bridge.csv",
    OUTPUTS / "carrier_invoices" / "australia_post_fuel_norms.csv",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Stable snapshot label, for example cp_july_2026.")
    args = parser.parse_args()
    if not args.label.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Snapshot labels may contain letters, numbers, hyphens, and underscores only.")

    missing_files = [path for path in SNAPSHOT_FILES if not path.exists()]
    if missing_files:
        raise FileNotFoundError("Cannot create snapshot; missing: " + ", ".join(str(path) for path in missing_files))

    destination = SNAPSHOT_ROOT / args.label
    if destination.exists():
        raise FileExistsError(f"Snapshot already exists: {destination}")
    destination.mkdir(parents=True)
    for source in SNAPSHOT_FILES:
        shutil.copy2(source, destination / source.name)
    for source in OPTIONAL_SNAPSHOT_FILES:
        if source.exists():
            shutil.copy2(source, destination / source.name)
    print(f"Created reconciliation snapshot: {destination}")


if __name__ == "__main__":
    main()
