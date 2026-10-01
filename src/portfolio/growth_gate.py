"""Growth gate — ONE definition of "is this a growth company", used twice.

The growth tier is meant to hold names that have BOTH quality and growth; if either is missing
the name is not wanted (Rain, 2026-10-01). Before this module the tier was the top 60 by a
weighted score in which growth carried 25% and had no floor, so a high-quality business that
had stopped growing (MKTX: 5%/yr, 3% trailing) sat comfortably inside it — quality fully
compensated for missing growth. The growth measurement itself was also wrong four ways:

  * the "5yr CAGR" was total growth divided by years (not compound), over four intervals —
    MELI read 77%/yr against a true 42%;
  * growth above 30% scored LOWER than 20-30%, and the inflated figure pushed real growers there;
  * the consistency term used abs(), so ACCELERATION was punished exactly like deceleration;
  * 40 of 100 points were awarded for growth being merely positive and steady.

This module is pure (no I/O) so the two enforcement points cannot drift:

  1. tools/screen_universe.py — the monthly screen. A growth-tier candidate must pass
     `passes_entry_gate` (durable growth >= GROWTH_FLOOR_PCT and quality >= QUALITY_FLOOR) before
     it is ranked. The tier may hold fewer than its nominal size; no filler.
  2. src/portfolio/scheduler.py — the monthly holdings review, which only MAKES SUGGESTIONS
     (cards for manual approval; it never sells). A held growth name gets a sell / covered-call
     card only when it is a `real_dropoff`: it fails the growth floor AND the most recent
     half-year confirms it. A name that fails the floor on history but is growing again
     (AAPL: 3%/yr over four years, 14% trailing) is frozen by the screen and gets no card.

Durable growth = the mean of the multi-year compound rate and the trailing-12-month rate, capped
at DECEL_CAP x the weaker of the two. The cap is the "growing over the history AND growing now"
requirement: one strong window cannot carry a dead one (LULU: 15%/yr history, 2% trailing ->
durable 2.6%), while acceleration is never penalised below the plain mean.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

GROWTH_FLOOR_PCT = 8.0   # durable revenue growth a growth-tier name must clear, % per year
QUALITY_FLOOR = 50.0     # quality pillar (0-100) a growth-tier name must clear
DECEL_CAP = 1.5          # durable growth may not exceed this multiple of the weaker window

# Growth strength, 0-100: monotone, continuous (no bucket cliffs -> no month-to-month churn at a
# boundary), saturating at 30%/yr. More growth never scores less.
_CURVE = ((0.0, 0.0), (5.0, 20.0), (10.0, 50.0), (15.0, 70.0), (20.0, 85.0), (30.0, 100.0))

# Quality pillar = the four non-growth sub-scores at their existing relative weights.
_Q_WEIGHTS = (25.0, 20.0, 15.0, 15.0)  # compounding, operating leverage, innovation, capital eff.


def compound_growth_pct(revenues_newest_first: Sequence[Optional[float]]) -> Optional[float]:
    """True compound annual growth, %, across a newest-first list of annual revenues.
    None when it cannot be computed (fewer than two usable years, or a non-positive endpoint)."""
    rev = [r for r in revenues_newest_first if r is not None]
    if len(rev) < 2:
        return None
    latest, oldest, years = rev[0], rev[-1], len(rev) - 1
    if latest is None or oldest is None or latest <= 0 or oldest <= 0:
        return None
    return ((latest / oldest) ** (1.0 / years) - 1.0) * 100.0


def simple_avg_to_compound_pct(simple_avg_pct: Optional[float], years: int = 4) -> Optional[float]:
    """Convert the legacy `revenue_avg_pct` (total growth / years) to a compound rate. Used only
    where the raw annual revenues are not available (cached / fallback fundamentals)."""
    if simple_avg_pct is None or years <= 0:
        return None
    total = 1.0 + simple_avg_pct * years / 100.0
    if total <= 0:
        return -100.0
    return (total ** (1.0 / years) - 1.0) * 100.0


def ttm_growth_pct(quarterly_revenues_newest_first: Sequence[Optional[float]]) -> Optional[float]:
    """Trailing-12-month revenue vs the 12 months before it, %. Needs eight quarters."""
    q = [(x or 0.0) for x in quarterly_revenues_newest_first]
    if len(q) < 8:
        return None
    now, then = sum(q[0:4]), sum(q[4:8])
    if then <= 0:
        return None
    return (now - then) / then * 100.0


def recent_half_growth_pct(quarterly_revenues_newest_first: Sequence[Optional[float]]) -> Optional[float]:
    """Growth of the latest two quarters over the same two quarters a year earlier, %.
    Needs six quarters. This is the 'is the trend still true right now' check for the sell path."""
    q = [(x or 0.0) for x in quarterly_revenues_newest_first]
    if len(q) < 6:
        return None
    now, then = q[0] + q[1], q[4] + q[5]
    if then <= 0:
        return None
    return (now - then) / then * 100.0


def durable_growth_pct(cagr_pct: Optional[float], ttm_pct: Optional[float]) -> Optional[float]:
    """Mean of the two windows, capped at DECEL_CAP x the weaker one (floored at 0).
    With only one window available that window is used as-is; with neither, None."""
    if cagr_pct is None and ttm_pct is None:
        return None
    if cagr_pct is None:
        return ttm_pct
    if ttm_pct is None:
        return cagr_pct
    mean = (cagr_pct + ttm_pct) / 2.0
    weaker = min(cagr_pct, ttm_pct)
    return min(mean, max(weaker, 0.0) * DECEL_CAP)


def growth_strength_score(durable_pct: Optional[float]) -> float:
    """0-100 growth strength from durable growth. Unknown growth scores a neutral 50 so the
    blended score stays comparable — the entry gate, not this score, rejects unknown growth."""
    if durable_pct is None:
        return 50.0
    x = durable_pct
    if x <= _CURVE[0][0]:
        return _CURVE[0][1]
    for (x0, y0), (x1, y1) in zip(_CURVE, _CURVE[1:]):
        if x <= x1:
            return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 1)
    return _CURVE[-1][1]


def quality_pillar(compounding: float, operating_leverage: float,
                   innovation: float, capital_efficiency: float) -> float:
    """0-100 quality pillar: the four non-growth sub-scores at their existing relative weights."""
    parts = (compounding, operating_leverage, innovation, capital_efficiency)
    return round(sum(p * w for p, w in zip(parts, _Q_WEIGHTS)) / sum(_Q_WEIGHTS), 1)


@dataclass(frozen=True)
class GrowthVerdict:
    durable_pct: Optional[float]
    cagr_pct: Optional[float]
    ttm_pct: Optional[float]
    recent_half_pct: Optional[float]
    passes_growth_floor: bool   # durable growth known and >= GROWTH_FLOOR_PCT
    real_dropoff: bool          # fails the floor AND the latest half-year confirms it
    reason: str


def growth_verdict(cagr_pct: Optional[float], ttm_pct: Optional[float],
                   recent_half_pct: Optional[float] = None,
                   floor_pct: float = GROWTH_FLOOR_PCT) -> GrowthVerdict:
    """Classify a name's growth.

    passes_growth_floor  — entry test for the growth tier. Unknown growth FAILS (a name whose
                           growth cannot be shown is, by the tier's rule, a name without growth).
    real_dropoff         — sell-side test. Requires the floor to be failed AND the most recent
                           half-year to be below the floor too. Unknown recent data => NOT a
                           drop-off (fail closed: no sell card on missing evidence).
    """
    durable = durable_growth_pct(cagr_pct, ttm_pct)
    fmt = lambda v: "n/a" if v is None else f"{v:+.1f}%"
    nums = (f"durable {fmt(durable)} (history {fmt(cagr_pct)}/yr, trailing 12m {fmt(ttm_pct)}, "
            f"latest half-year {fmt(recent_half_pct)}; floor {floor_pct:.0f}%)")
    if durable is None:
        return GrowthVerdict(None, cagr_pct, ttm_pct, recent_half_pct, False, False,
                             "growth unknown — no revenue history")
    if durable >= floor_pct:
        return GrowthVerdict(durable, cagr_pct, ttm_pct, recent_half_pct, True, False,
                             f"growing: {nums}")
    if recent_half_pct is None:
        return GrowthVerdict(durable, cagr_pct, ttm_pct, recent_half_pct, False, False,
                             f"below growth floor, latest half-year unknown: {nums}")
    if recent_half_pct >= floor_pct:
        return GrowthVerdict(durable, cagr_pct, ttm_pct, recent_half_pct, False, False,
                             f"below growth floor but re-accelerating: {nums}")
    return GrowthVerdict(durable, cagr_pct, ttm_pct, recent_half_pct, False, True,
                         f"below growth floor and the latest half-year confirms it: {nums}")


def passes_entry_gate(durable_pct: Optional[float], quality: Optional[float],
                      floor_pct: float = GROWTH_FLOOR_PCT,
                      quality_floor: float = QUALITY_FLOOR) -> tuple[bool, str]:
    """Growth-tier entry: BOTH pillars must be present. Returns (ok, reason-if-not)."""
    if durable_pct is None:
        return False, "growth unknown"
    if durable_pct < floor_pct:
        return False, f"durable growth {durable_pct:.1f}% < {floor_pct:.0f}% floor"
    if quality is None or quality < quality_floor:
        q = "n/a" if quality is None else f"{quality:.0f}"
        return False, f"quality {q} < {quality_floor:.0f} floor"
    return True, ""
