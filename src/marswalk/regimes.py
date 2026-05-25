"""Load the MarsWalk regime definitions + backtest universe from YAML."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_PATH = Path("config/marswalk_regimes.yaml")


@dataclass
class Regime:
    id: str
    rank: int
    name: str
    category: str
    start: str   # YYYY-MM-DD inclusive
    end: str     # YYYY-MM-DD inclusive
    why: str = ""


def load_config(path: Path | None = None) -> tuple[list[str], list[Regime]]:
    """Return (universe, regimes) with regimes sorted by rank (1 = most critical)."""
    p = path or _PATH
    data = yaml.safe_load(p.read_text()) or {}
    universe = list(data.get("universe", []))
    regimes = [Regime(**r) for r in data.get("regimes", [])]
    regimes.sort(key=lambda r: r.rank)
    return universe, regimes


def get_regime(regime_id: str) -> Regime | None:
    _, regimes = load_config()
    for r in regimes:
        if r.id == regime_id:
            return r
    return None
