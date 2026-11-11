from src.stage3_moorebank_cost_estimator import _goexpress_band_cost, _goexpress_weight_cap_details


def test_goexpress_above_20kg_uses_top_published_band_and_is_flagged():
    bands = {("TRUG", "Same State"): [(0.0, 0.5, 4.0), (19.0, 20.0, 6.0)]}

    assert _goexpress_band_cost("TRUG", "Same State", 20.01, bands) == 6.0
    assert _goexpress_weight_cap_details("TRUG", "Same State", [20.01], bands) == (
        "Y",
        20.0,
        "Parcel above published GoEXPRESS 20.00 kg ceiling; top published band applied.",
    )
