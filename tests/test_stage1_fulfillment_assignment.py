from datetime import datetime

from src.stage1_moorebank_parcel_assigner import PackedRow, _build_query, _source_parcel_assignment
from src.stage1_truganina_parcel_assigner import _build_query as _truganina_build_query


def _row(*, sku_id: str, package_id: str, shipment_id: str = "", quantity: int = 1) -> PackedRow:
    return PackedRow(
        invoice_number="5801700011",
        actual_parcel_count=3,
        actual_carrier="Australia Post",
        sku_id=sku_id,
        rbu_description="Unclassified",
        product_description="",
        quantity_packed=quantity,
        pack_timestamp=datetime(2026, 8, 30),
        height=10.0,
        width=10.0,
        length=10.0,
        weight=1.0,
        cubage=1000.0,
        source_cubage=1000.0,
        size_category="Toteable",
        source_shipment_id=shipment_id,
        source_package_id=package_id,
        source_package_detail_id="1",
        assignment_source="ORD_FULFILLMENT_DETAIL",
    )


def test_fulfillment_package_ids_allocate_units_to_their_source_parcels() -> None:
    rows = [
        _row(sku_id="43659913", package_id="00693411108803893969"),
        _row(sku_id="43363889", package_id="00693411108803914619"),
        _row(sku_id="43363889", package_id="00693411108803915425"),
    ]

    result = _source_parcel_assignment(rows, actual_parcel_count=3)

    assert result is not None
    parcels, rule_name, estimated_count = result
    assert rule_name == "FULFILLMENT_DETAIL_PACKAGE_ALLOCATION"
    assert estimated_count == 3
    assert [parcel.total_units for parcel in parcels] == [1, 1, 1]
    assert [[line.sku_id for line in parcel.lines] for parcel in parcels] == [
        ["43659913"],
        ["43363889"],
        ["43363889"],
    ]


def test_fulfillment_allocation_falls_back_when_package_count_is_incomplete() -> None:
    rows = [
        _row(sku_id="43659913", package_id="00693411108803893969"),
        _row(sku_id="43363889", package_id="00693411108803914619"),
    ]

    assert _source_parcel_assignment(rows, actual_parcel_count=3) is None


def test_fulfillment_allocation_can_use_shipments_when_package_ids_are_missing() -> None:
    rows = [
        _row(sku_id="43659913", package_id="", shipment_id="shipment-1"),
        _row(sku_id="43363889", package_id="", shipment_id="shipment-2"),
        _row(sku_id="43363889", package_id="", shipment_id="shipment-3"),
    ]

    result = _source_parcel_assignment(rows, actual_parcel_count=3)

    assert result is not None
    assert result[1] == "FULFILLMENT_DETAIL_SHIPMENT_ALLOCATION"


def test_stage1_queries_include_auditable_fulfillment_fallback() -> None:
    for query in (_build_query(), _truganina_build_query()):
        assert "KSFPA.OMFP.ORD_FULFILLMENT_DETAIL" in query
        assert "COUNT(" in query
        assert "NULLIF(TRIM(SOURCE_SHIPMENT_ID), '')" in query
        assert "FROM packed_base p" in query or "FROM packed p" in query
        assert "'CATCH_CFC.PACKED' AS ASSIGNMENT_SOURCE" in query
        assert "'ORD_FULFILLMENT_DETAIL' AS ASSIGNMENT_SOURCE" in query
