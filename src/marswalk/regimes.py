"""Load the MarsWalk regime definitions + backtest universe from YAML."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_PATH = Path("config/marswalk_regimes.yaml")


@dataclass
class HistoricalAnalog:
    """Real historical window used as a backtest proxy for a forward scenario
    whose own dates haven't arrived yet. The data layer fetches THIS window
    and stores it under the regime's id, so the engine doesn't see a difference."""
    start: str           # YYYY-MM-DD
    end: str             # YYYY-MM-DD
    label: str = ""      # human-readable, shown on the regime card


@dataclass
class Regime:
    id: str
    rank: int
    name: str
    category: str
    start: str   # YYYY-MM-DD inclusive
    end: str     # YYYY-MM-DD inclusive
    why: str = ""
    historical_analog: HistoricalAnalog | None = None
    # Per-name ticker substitution: fetch bars from the proxy ticker but store
    # them under today's universe key. Used by `ai_crash` to drape today's
    # 47-name universe over the 2000-02 dot-com bust on a name-by-name basis.
    # Names not in the dict fetch their own real bars.
    proxy_universe: dict[str, str] | None = None

    def effective_window(self, today: str | None = None) -> tuple[str, str, bool]:
        """Return (start, end, is_analog). If `start > today` and an analog is
        configured, fall through to the analog window. `today` defaults to the
        actual current date."""
        import datetime as _dt
        if today is None:
            today = _dt.date.today().isoformat()
        if self.historical_analog and self.start > today:
            a = self.historical_analog
            return a.start, a.end, True
        return self.start, self.end, False


def _parse_regime(r: dict) -> Regime:
    """Build a Regime from a YAML row, lifting nested historical_analog block."""
    analog_block = r.pop("historical_analog", None)
    analog = HistoricalAnalog(**analog_block) if analog_block else None
    return Regime(historical_analog=analog, **r)


def load_config(path: Path | None = None) -> tuple[list[str], list[Regime]]:
    """Return (universe, regimes) with regimes sorted by rank (1 = most critical)."""
    p = path or _PATH
    data = yaml.safe_load(p.read_text()) or {}
    universe = list(data.get("universe", []))
    regimes = [_parse_regime(r) for r in data.get("regimes", [])]
    regimes.sort(key=lambda r: r.rank)
    return universe, regimes


def get_regime(regime_id: str) -> Regime | None:
    _, regimes = load_config()
    for r in regimes:
        if r.id == regime_id:
            return r
    return None
