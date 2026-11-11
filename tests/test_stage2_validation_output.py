import csv
import io
from pathlib import Path
from unittest.mock import mock_open, patch

import src.stage2_moorebank_parcel_physical_estimator as stage2
from src.stage2_moorebank_parcel_physical_estimator import ParcelPhysical, _write_validation_output


def test_validation_output_allows_missing_shippit_metadata() -> None:
    row = ParcelPhysical(
        invoice_number="1234567890",
        actual_carrier="Couriers Please",
        actual_parcel_count=1,
        estimated_parcel_count=1,
        rule_name="FALLBACK_ROUND_ROBIN",
        process_path="TOTEABLE",
        estimated_pack_origin_path="Fallback",
        parcel_contents_size_mix="Toteable only",
        parcel_id=1,
        placeholder_parcel="",
        parcel_units=1,
        parcel_weight_kg=1.0,
        parcel_cube_cm3=1000.0,
        packed_sku_cube_cm3=1000.0,
        cube_uplift_cm3=0.0,
        estimated_length_cm=10.0,
        estimated_width_cm=10.0,
        estimated_height_cm=10.0,
        estimated_shape_source="CFC_DIMS",
        cfc_cube_imputed="",
        parcel_sku_qty_pattern="1",
        parcel_packed_sku_cube_pattern_cm3="1000.00",
        fit_band="TOTE_LIKE",
        sku_ids="SKU-1",
    )
    output_path = Path("stage2_moorebank_parcel_physicals_validation.csv")
    file_mock = mock_open()
    with patch.object(Path, "mkdir"), patch.object(Path, "open", file_mock):
        validation_rows = _write_validation_output(output_path, [row], {})

        assert validation_rows[0]["shippit_dead_weight_kg"] is None
        assert validation_rows[0]["est_weight_gap_vs_shippit_dead_kg"] is None
        written = "".join(call.args[0] for call in file_mock().write.call_args_list)
        written_row = next(csv.DictReader(io.StringIO(written)))
        assert written_row["shippit_dead_weight_kg"] == ""
        assert written_row["est_weight_gap_vs_shippit_dead_kg"] == ""


def test_order_metadata_batches_large_invoice_lists() -> None:
    class Query:
        def collect(self):
            return []

    class Session:
        def __init__(self):
            self.queries = []

        def sql(self, query):
            self.queries.append(query)
            return Query()

        def close(self):
            pass

    session = Session()
    invoices = [str(value) for value in range(5001)]
    with patch.object(stage2, "get_session", return_value=session):
        assert stage2._load_order_meta(invoices) == {}

    assert len(session.queries) == 2
    assert all("FROM VALUES" in query for query in session.queries)
    assert all(" IN (" not in query for query in session.queries)
