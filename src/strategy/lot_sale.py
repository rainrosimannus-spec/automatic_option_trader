"""Execution rules for a wheel lot sale (stop, package unwind, live exit) — 2026-11-24 (Rain).

Until today every lot sale was a DAY limit at the bid of one quote snapshot and was never
touched again: ADSK's exit sale left the quote at 257.59, reached IBKR twelve seconds later
under the bid, and rested unfilled into an earnings day while every later scan returned early
on the "submitted" marker. Two rules fix that, both pure here so the live code and the tests
share one source:

  * `sale_limit`      — price a new sale a hair UNDER the bid so it is marketable on arrival
                        (bid × (1 − pad), at least one tick below the bid).
  * `reprice_decision` — every scan re-judges a resting sale against a fresh two-sided quote:
                        "modify" to the new marketable limit, "cancel" an EXIT sale whose
                        condition is gone (mid back under the assignment level + fee), "keep"
                        when the quote is unusable or the limit already sits where it should.
                        An exit sale is never re-priced below its floor; stop/unwind sales
                        are liquidations already decided (the call may already be bought back)
                        and simply follow the bid.

`unwind_min_profit` is the earnings-eve knob on Rule C: the package unwind normally wants the
package to beat net basis by 0.5%; the day before (or of) a print it may exit flat instead of
carrying a capped lot through the event. Live-only (MarsWalk has no intraday quotes nor an
earnings calendar) — the dashboard chips say so.
"""
from __future__ import annotations

TICK = 0.01
SPREAD_MAX = 0.02           # same two-sided-quote guard the exit / stop / unwind already use
EXIT_SOURCE = "wheel_exit"
LIQUIDATION_SOURCES_ALL = ("wheel_stop", "wheel_unwind", EXIT_SOURCE)


def sale_limit(bid: float | None, pad_pct: float) -> float | None:
    """Marketable sell limit: bid × (1 − pad), never closer than one tick under the bid."""
    if not bid or bid <= 0:
        return None
    pad = max(0.0, float(pad_pct or 0.0))
    return round(min(bid - TICK, bid * (1.0 - pad)), 2)


def reprice_decision(source: str, bid: float | None, ask: float | None, current_limit: float | None,
                     *, exit_floor: float | None = None, pad_pct: float = 0.0005,
                     spread_max: float = SPREAD_MAX) -> tuple[str, float | None]:
    """("keep"|"modify"|"cancel", new_limit). See module docstring."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return "keep", None
    mid = (bid + ask) / 2.0
    if (ask - bid) / mid >= spread_max:
        return "keep", None
    new = sale_limit(bid, pad_pct)
    if source == EXIT_SOURCE and exit_floor is not None:
        if mid < exit_floor:
            return "cancel", None                  # exit condition gone — write the call instead
        new = max(new, round(exit_floor, 2))       # never sell an exit below its bar
    if current_limit is not None and abs(new - float(current_limit)) < TICK / 2:
        return "keep", None
    return "modify", new


def unwind_min_profit(base_pct: float, earnings_soon: bool, enabled: bool) -> float:
    """Rule C profit bar: 0 on earnings eve when the knob is on, else the configured bar."""
    if enabled and earnings_soon:
        return 0.0
    return float(base_pct)
