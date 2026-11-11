from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
CONFIG_DIR = ROOT_DIR / "config"
WEB_DIR = ROOT_DIR / "web"


def default_data_dir() -> Path:
    """Personal data (database, secrets file, reports). Ignored by git."""
    return Path(os.environ.get("CRUMENAE_DATA_DIR") or os.environ.get("CRYPTOLEDGER_DATA_DIR")
                or (ROOT_DIR / "data")).expanduser()
