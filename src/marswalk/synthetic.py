"""Synthetic transforms applied to fetched market data before the engine sees it.

These mutate the {symbol: [(date, close, iv), ...]} dataset to simulate events
that don't exist in the historical record. Currently:

  - apply_halts: model an exchange blackout (cyber/grid/power-outage) by
    dropping bars in halt windows and applying a permanent gap shift on every
    post-halt bar + a one-day vol spike. Used by the `blackout_3day` regime.

  - apply_shocks: model a single-day surprise crash (e.g. a 2nd Lehman) by
    applying a permanent equity gap from the shock date forward, with a
    one-day vol spike on the shock date itself. Used by `stacked_2x` to
    overlay a 2nd shock on top of gfc_2008.

Pure/deterministic: same input → same output. No I/O.
"""
from __future__ import annotations

import datetime as _dt


def apply_halts(market: dict, halts: list[dict],
                gap_open_pct: float = 0.0,
                iv_bump: float = 2.0) -> dict:
    """Drop halt-window bars; permanently shift post-halt equity prices; one-day
    IV bump on the first post-halt bar.

    market: {symbol: [(date_obj, close, iv), ...]} as built by mw_data.load_market.
    halts:  list of {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} inclusive ranges.
    gap_open_pct: e.g. -0.30 → -30% PERMANENT price shift applied to every
                  post-halt equity close (the new baseline; subsequent days
                  trade from there). Default 0 = no gap, just halt days.
    iv_bump: one-time multiplier on the FIRST post-halt bar — applied to equity
             IV (vol spike on reopen) and to ^VIX close (VIX is itself implied
             vol). Subsequent post-halt bars revert to historical IV/VIX since
             vol mean-reverts within days even after a real shock.

    `_pre:<sym>` warmup bars pass through untouched — they seed per-name MA200
    before the regime starts; halts only affect the live window.

    With multiple halt windows the equity gap composes multiplicatively (two
    -30% gaps → a permanent ~-51% level vs original baseline)."""
    halt_dates: set[_dt.date] = set()
    halt_ends: list[_dt.date] = []
    for halt in halts:
        s = _dt.date.fromisoformat(halt["start"])
        e = _dt.date.fromisoformat(halt["end"])
        halt_ends.append(e)
        cur = s
        while cur <= e:
            halt_dates.add(cur)
            cur += _dt.timedelta(days=1)
    halt_ends.sort()

    new_market: dict = {}
    for sym, bars in market.items():
        if sym.startswith("_pre:"):
            new_market[sym] = bars
            continue
        kept = [b for b in bars if b[0] not in halt_dates]
        kept.sort(key=lambda b: b[0])

        for he in halt_ends:
            first_after_idx = None
            for i, b in enumerate(kept):
                if b[0] <= he:
                    continue
                if first_after_idx is None:
                    first_after_idx = i
                bd, close, iv = b
                if sym == "^VIX":
                    # VIX one-time spike on the first post-halt bar; mean-reverts after.
                    if i == first_after_idx:
                        kept[i] = (bd, close * iv_bump, iv)
                else:
                    # Equity: permanent price-level shift on EVERY post-halt close.
                    new_close = close * (1.0 + gap_open_pct)
                    new_iv = iv
                    if i == first_after_idx and iv is not None and iv > 0:
                        new_iv = iv * iv_bump  # one-day vol spike
                    kept[i] = (bd, new_close, new_iv)
        new_market[sym] = kept
    return new_market


def apply_shocks(market: dict, shocks: list[dict], iv_bump: float = 2.0) -> dict:
    """Apply single-day equity-price shocks with permanent forward shift.

    market: same shape as apply_halts input.
    shocks: list of {"date": "YYYY-MM-DD", "pct": -0.15} entries. From shock
            date inclusive onward, every equity bar's close is multiplied by
            (1+pct) — permanent level shift. On the shock date itself, equity
            IV and ^VIX close are bumped iv_bump× to model the one-day vol
            spike. Stacks multiplicatively when multiple shocks compound.

    Effectively a 0-day halt + gap_open: no bars are dropped, just a price
    discontinuity applied from a date forward. Used by `stacked_2x` to overlay
    a 2nd shock on gfc_2008's natural drawdown."""
    if not shocks:
        return market
    shock_list = sorted(
        [(_dt.date.fromisoformat(s["date"]), float(s["pct"])) for s in shocks],
        key=lambda x: x[0]
    )
    new_market: dict = {}
    for sym, bars in market.items():
        if sym.startswith("_pre:"):
            new_market[sym] = bars
            continue
        bars_sorted = sorted(bars, key=lambda b: b[0])
        for shock_date, pct in shock_list:
            for i, b in enumerate(bars_sorted):
                bd, close, iv = b
                if bd < shock_date:
                    continue
                if sym == "^VIX":
                    if bd == shock_date:
                        bars_sorted[i] = (bd, close * iv_bump, iv)
                else:
                    new_close = close * (1.0 + pct)
                    new_iv = iv
                    if bd == shock_date and iv is not None and iv > 0:
                        new_iv = iv * iv_bump
                    bars_sorted[i] = (bd, new_close, new_iv)
        new_market[sym] = bars_sorted
    return new_market
