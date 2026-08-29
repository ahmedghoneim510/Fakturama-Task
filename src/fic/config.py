"""Load config/app.yaml and config/selectors.yaml. Both are plain data -- no
coordinates, ever -- and are read fresh at startup so editing a label or a timeout
never requires a code change."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


@lru_cache(maxsize=1)
def app_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "app.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def selectors() -> dict:
    return yaml.safe_load((CONFIG_DIR / "selectors.yaml").read_text(encoding="utf-8"))


def money_tolerance() -> Decimal:
    return Decimal(str(app_config()["matching"]["money_tolerance"]))


def ambiguity_margin_px() -> int:
    return int(app_config()["grounding"]["ambiguity_margin_px"])


def date_format() -> str:
    """strftime pattern this Fakturama install's Date field actually expects
    (see config/app.yaml locale.date_format -- confirmed live, not a constant)."""
    return app_config()["locale"]["date_format"]


def payment_code_for(method: str) -> str | None:
    """Brief S2.10.4's closed mapping. Returns None for an unmapped method --
    callers must treat that as OptionUnavailable, never guess a code."""
    return app_config()["payment_code_map"].get(method)
