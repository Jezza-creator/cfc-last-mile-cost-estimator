from __future__ import annotations

import os
from pathlib import Path

import src.stage3_moorebank_cost_estimator as base
from src.site_config import SITE_CONFIGS


base.STORE_NAME = SITE_CONFIGS["TRUGANINA"].store_name
output_dir = Path(os.environ.get("LM_OUTPUT_DIR", "outputs"))
base.INPUT_PATH = Path(os.environ.get("LM_STAGE2_INPUT_PATH", output_dir / "stage2_truganina_parcel_physicals_full.csv"))
base.OUTPUT_PATH = Path(os.environ.get("LM_STAGE3_OUTPUT_PATH", output_dir / "stage3_truganina_cost_estimates_full.csv"))


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
