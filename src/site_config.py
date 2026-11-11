from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SiteConfig:
    site_key: str
    site_label: str
    store_name: str
    inventory_warehouse_code: str
    goexpress_warehouse_code: str
    cp_origin_zone: str
    ap_origin_state: str
    ap_origin_area: str
    gobundle_site_key: str
    allied_origin_zone: str


SITE_CONFIGS: dict[str, SiteConfig] = {
    "MOOREBANK": SiteConfig(
        site_key="MOOREBANK",
        site_label="Moorebank",
        store_name="1194 Moorebank CFC NSW AU",
        inventory_warehouse_code="SYDM01",
        goexpress_warehouse_code="SYDM01",
        cp_origin_zone="SYD",
        ap_origin_state="NSW",
        ap_origin_area="Sydney",
        gobundle_site_key="MOOREBANK",
        allied_origin_zone="N01",
    ),
    "TRUGANINA": SiteConfig(
        site_key="TRUGANINA",
        site_label="Truganina",
        store_name="1190 Truganina CFC VIC AU",
        inventory_warehouse_code="TRUG",
        goexpress_warehouse_code="TRUG",
        cp_origin_zone="MEL",
        ap_origin_state="VIC",
        ap_origin_area="Melbourne",
        gobundle_site_key="TRUGANINA",
        allied_origin_zone="V01",
    ),
}


def get_site_config(site_key: str) -> SiteConfig:
    site = str(site_key or "").strip().upper()
    if site not in SITE_CONFIGS:
        raise ValueError(f"Unsupported site key: {site_key}")
    return SITE_CONFIGS[site]


def get_site_config_for_store_name(store_name: str) -> SiteConfig:
    store = str(store_name or "").strip().upper()
    if "TRUGANINA" in store or store.startswith("1190"):
        return SITE_CONFIGS["TRUGANINA"]
    if "MOOREBANK" in store or store.startswith("1194"):
        return SITE_CONFIGS["MOOREBANK"]
    return SITE_CONFIGS["MOOREBANK"]
