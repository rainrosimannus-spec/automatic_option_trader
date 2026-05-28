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
    # Extra symbols appended to the base universe for THIS regime only. Used to
    # supplement old regimes (2008-2011) with broad-market names (JPM, XOM, JNJ
    # etc.) that the live screener wouldn't pick today but existed then —
    # tests how a sector-diversified wheel would have fared. Other regimes are
    # untouched (extension=None).
    universe_extension: list[str] | None = None
    # Synthetic exchange-blackout windows. Each entry is {"start": "YYYY-MM-DD",
    # "end": "YYYY-MM-DD"} — inclusive halted-trading dates. The service layer
    # drops every bar in these windows (across every symbol incl. ^SPY/^VIX) so
    # the engine sees a date-axis gap and trades nothing during the halt. Used
    # by `blackout_3day` to model cyber/grid/power-outage events. None for most.
    halts: list[dict] | None = None
    # One-time price discontinuity applied to the FIRST surviving bar after EACH
    # halt window: equity close *= (1 + gap_open_pct); equity IV *= 2.0 to model
    # the vol-spike on resumption; ^VIX close *= 2.0 (VIX is itself implied vol).
    # e.g. -0.30 = -30% gap-down. Ignored unless `halts` is set.
    gap_open_pct: float | None = None
    # Synthetic one-day price shocks (no halt period). Each entry is
    # {"date": "YYYY-MM-DD", "pct": -0.15}. From shock date forward every equity
    # bar gets a permanent close shift × (1+pct); IV/VIX are bumped 2× on the
    # shock date only. Used by `stacked_2x` to overlay a 2nd Lehman-class event
    # on top of gfc_2008's natural drawdown — tests correlation-breakdown defenses.
    shocks: list[dict] | None = None
    # Multiplicative price scaler for OLD regimes whose nominal price levels
    # (1970s blue chips at $4-30) are too low to interact meaningfully with the
    # wheel's premium minimums + $4M NLV scale. Used by stagflation_70s with
    # ~7.5x (1973→2024 CPI ratio) so the engine sees today-scale prices while
    # day-over-day RETURNS remain identical. Default 1.0 = no scaling.
    price_multiplier: float = 1.0

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
