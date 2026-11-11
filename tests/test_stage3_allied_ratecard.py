from pathlib import Path

from src.site_config import SITE_CONFIGS
from src.stage3_moorebank_cost_estimator import _load_allied_ecom_cfc_rates


RATECARD_PATH = Path("data/reference/allied_ecom_cfc_ratecard.csv")


def test_allied_ecom_ratecard_has_site_specific_origin_rates():
    moorebank = _load_allied_ecom_cfc_rates(RATECARD_PATH, "MOOREBANK")
    truganina = _load_allied_ecom_cfc_rates(RATECARD_PATH, "TRUGANINA")

    assert moorebank["N01"] == {
        "minimum": 12.77,
        "basic": 10.82,
        "per_kg": 0.2,
    }
    assert truganina["V01"] == {
        "minimum": 12.77,
        "basic": 10.82,
        "per_kg": 0.2,
    }
    assert moorebank["V01"] != truganina["V01"]


def test_allied_site_configs_use_their_cfc_origin_zones():
    assert SITE_CONFIGS["MOOREBANK"].allied_origin_zone == "N01"
    assert SITE_CONFIGS["TRUGANINA"].allied_origin_zone == "V01"
