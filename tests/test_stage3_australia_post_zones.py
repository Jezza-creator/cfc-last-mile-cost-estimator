from src import stage3_moorebank_cost_estimator as stage3


def test_truganina_uses_same_state_categories_for_victoria(monkeypatch):
    monkeypatch.setattr(stage3, "STORE_NAME", "1190 Truganina CFC VIC AU")
    area_to_zone = {
        "Melbourne": "Near State Capital",
        "VIC Metro": "Near State Metro",
        "VIC Country": "Near State Remote",
    }

    assert stage3._ap_pricing_zone("Melbourne", "VIC", area_to_zone) == "Same State Local"
    assert stage3._ap_pricing_zone("VIC Metro", "VIC", area_to_zone) == "Same State Metro"
    assert stage3._ap_pricing_zone("VIC Country", "VIC", area_to_zone) == "Same State Remote"


def test_truganina_uses_near_state_categories_for_new_south_wales(monkeypatch):
    monkeypatch.setattr(stage3, "STORE_NAME", "1190 Truganina CFC VIC AU")
    area_to_zone = {
        "Sydney": "Same State Local",
        "NSW Metro": "Same State Metro",
        "NSW Country": "Same State Remote",
    }

    assert stage3._ap_pricing_zone("Sydney", "NSW", area_to_zone) == "Near State Capital"
    assert stage3._ap_pricing_zone("NSW Metro", "NSW", area_to_zone) == "Near State Metro"
    assert stage3._ap_pricing_zone("NSW Country", "NSW", area_to_zone) == "Near State Remote"


def test_moorebank_keeps_sydney_origin_categories(monkeypatch):
    monkeypatch.setattr(stage3, "STORE_NAME", "1194 Moorebank CFC NSW AU")
    area_to_zone = {"Sydney": "Same State Local", "Melbourne": "Near State Capital"}

    assert stage3._ap_pricing_zone("Sydney", "NSW", area_to_zone) == "Same State Local"
    assert stage3._ap_pricing_zone("Melbourne", "VIC", area_to_zone) == "Near State Capital"
