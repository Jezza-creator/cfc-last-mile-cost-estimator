from pathlib import Path

import pandas as pd

from src.opportunity_register import (
    OPPORTUNITY_COLUMNS,
    read_opportunity_register,
    upsert_opportunities,
)


def _distinct_affected_consignments(frame: pd.DataFrame) -> int:
    source_consignments = frame["source_consignment"].astype(str).str.strip()
    identified_consignments = source_consignments.loc[source_consignments.ne("")]
    aggregate_rows = frame.loc[source_consignments.eq("")]
    return int(
        identified_consignments.nunique()
        + pd.to_numeric(aggregate_rows["affected_consignment_count"], errors="coerce").sum()
    )


def test_register_has_stable_schema_and_reads_pilot() -> None:
    path = Path("outputs/opportunity_register.csv")

    register = read_opportunity_register(path)

    assert list(register.columns) == list(OPPORTUNITY_COLUMNS)
    assert set(register["opportunity_id"]) == {
        "OPP-TRUG-EPARCEL-42909484-WHS-ALLOCATION",
        "OPP-TRUG-EPARCEL-42909484-WHS-PHYSICAL-ESTIMATE",
    }
    assert register["source_consignment"].eq("6559050711").all()
    allocation = register.loc[
        register["opportunity_id"].eq("OPP-TRUG-EPARCEL-42909484-WHS-ALLOCATION")
    ].iloc[0]
    physical = register.loc[
        register["opportunity_id"].eq("OPP-TRUG-EPARCEL-42909484-WHS-PHYSICAL-ESTIMATE")
    ].iloc[0]
    assert pd.isna(allocation["potential_saving_ex_gst"])
    assert allocation["alternative_carrier_saving_signal_ex_gst"] == 15.12
    assert pd.isna(physical["potential_saving_ex_gst"])


def test_upsert_preserves_manual_review_fields() -> None:
    existing = pd.DataFrame(
        [
            {
                "opportunity_id": "OPP-1",
                "status": "In progress",
                "proposed_owner": "Operations",
                "modelled_shipped_cost_ex_gst": 10.0,
            }
        ]
    )
    update = pd.DataFrame(
        [
            {
                "opportunity_id": "OPP-1",
                "status": "New",
                "proposed_owner": "Master Data",
                "modelled_shipped_cost_ex_gst": 12.0,
            }
        ]
    )

    result = upsert_opportunities(existing, update)

    row = result.iloc[0]
    assert row["status"] == "In progress"
    assert row["proposed_owner"] == "Operations"
    assert row["modelled_shipped_cost_ex_gst"] == 12.0


def test_register_counts_one_consignment_once_across_linked_opportunities() -> None:
    register = read_opportunity_register(Path("outputs/opportunity_register.csv"))

    assert len(register) == 2
    assert _distinct_affected_consignments(register) == 1
