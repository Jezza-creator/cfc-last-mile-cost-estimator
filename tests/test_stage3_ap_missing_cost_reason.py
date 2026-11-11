from src import stage3_moorebank_cost_estimator as stage3


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def collect(self):
        return self._rows


class _FakeSession:
    def sql(self, query):
        if "SELECT DISTINCT" in query:
            return _FakeQuery([{"DESTINATION_AREA": "Sydney", "DESTINATION_ZONE": "Same State Local"}])
        if "LM_WEIGHT_BAND" in query:
            return _FakeQuery([])
        return _FakeQuery(
            [
                {
                    "RATECARD_ID": "AP_EPARCEL_20250805",
                    "PRICING_ZONE": "Same State Local",
                    "FIRST_PARCEL_RATE_EX_GST": 3.73,
                    "SUBSEQUENT_PARCEL_RATE_EX_GST": 2.36,
                    "PER_KG_RATE_EX_GST": 0.13,
                    "MINIMUM_CHARGE_EX_GST": 1.0,
                }
            ]
        )


def test_ap_missing_reason_uses_adjusted_chargeable_weights_for_lane_lookup():
    reason = stage3._actual_cost_missing_reason(
        "eParcel",
        "eParcel",
        None,
        {"AP": "Sydney"},
        {"ap_lane_rates": {}, "ap_weight_bands": {"Same State Local": [(0.0, 5.0, 10.0)]}},
        "Same State Local",
        None,
        0.0,
        [5.95, 0.71],
    )

    assert reason == "no_ap_lane_rate_match_over_5kg"


def test_ap_missing_reason_identifies_missing_under_5kg_band():
    reason = stage3._actual_cost_missing_reason(
        "eParcel",
        "eParcel",
        None,
        {"AP": "Sydney"},
        {"ap_lane_rates": {}, "ap_weight_bands": {"Same State Local": []}},
        "Same State Local",
        None,
        0.0,
        [4.95],
    )

    assert reason == "no_ap_weight_band_match_under_5kg"


def test_ap_cost_trims_pricing_zone_before_lane_lookup():
    cost = stage3._ap_cost(
        "Sydney",
        {},
        {},
        {
            "Same State Local": {
                "first": 4.15,
                "subsequent": 2.79,
                "per_kg": 1.03,
                "minimum": 0.0,
            }
        },
        [6.0],
        " Same State Local ",
    )

    assert cost == 10.33


def test_ap_lane_loader_uses_destination_zone_as_pricing_key():
    _, _, lane_rates = stage3._load_ap_rate_tables(_FakeSession(), "Sydney")

    assert lane_rates["Same State Local"]["first"] == 3.73
    assert lane_rates["Same State Local"]["per_kg"] == 0.13
