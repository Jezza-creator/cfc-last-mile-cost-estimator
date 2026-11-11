from __future__ import annotations

import os
from pathlib import Path

import src.stage2_moorebank_parcel_physical_estimator as base
from src.site_config import SITE_CONFIGS


base.STORE_NAME = SITE_CONFIGS["TRUGANINA"].store_name
output_dir = Path(os.environ.get("LM_OUTPUT_DIR", "outputs"))
base.INPUT_PATH = Path(os.environ.get("LM_STAGE1_INPUT_PATH", output_dir / "stage1_truganina_parcel_assignments_full.csv"))
base.PARCEL_CONTENTS_PATH = Path(os.environ.get("LM_STAGE1_PARCEL_CONTENTS_PATH", output_dir / "stage1_truganina_parcel_assignment_lines.csv"))
base.OUTPUT_PATH = Path(os.environ.get("LM_STAGE2_OUTPUT_PATH", output_dir / "stage2_truganina_parcel_physicals_full.csv"))


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
