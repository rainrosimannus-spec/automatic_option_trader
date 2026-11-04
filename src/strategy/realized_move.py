"""Realised-move helpers shared by the live put screen and the MarsWalk backtester.

Rain's account, first four months (2026-06 → 11): 164 puts at a median 2.5% OTM and 3 DTE
on names that fell 2.5%+ within 3 days 35-65% of the time this year; 33% finished ITM.
Two per-name, data-driven rules follow from that — no hand lists:

* `realized_otm_floor` — the put strike floor becomes max(flat floor, k × the 80th-percentile
  trailing 3-day decline), so a name that routinely moves 6% in three days is not sold a
  2.5%-OTM put.
* `breach_rate` — the fraction of recent days on which the next 3 closes dipped more than
  2.5% below the start; names above a ceiling are skipped by the put seller altogether.

All functions are pure over a list of closes (oldest → newest) so live (FMP history) and
the sim (its own bar series) compute the same number ([[live-marswalk-parity-rule]]).
"""
from __future__ import annotations

from typing import Sequence


def forward_declines(closes: Sequence[float], horizon: int = 3) -> list[float]:
    """For each start day i, the deepest decline over the next `horizon` closes:
    1 − min(closes[i+1 : i+1+horizon]) / closes[i]. Positive = a decline; negative values
    mean the lowest of the next closes was still above the start."""
    out: list[float] = []
    n = len(closes)
    for i in range(n - horizon):
        c0 = closes[i]
        if not c0 or c0 <= 0:
            continue
        window = closes[i + 1:i + 1 + horizon]
        if len(window) < horizon:
            break
        out.append(1.0 - min(window) / c0)
    return out


def _quantile(values: Sequence[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        raise ValueError("empty")
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def realized_otm_floor(closes: Sequence[float], *, k: float, lookback: int, pctl: float,
                       horizon: int, base_floor: float) -> float | None:
    """max(base_floor, k × pctl-quantile of the trailing `lookback` forward declines).

    None when fewer than lookback // 2 samples exist (caller falls back to the flat window)."""
    if not closes:
        return None
    tail = list(closes)[-(lookback + horizon):]
    decl = forward_declines(tail, horizon)
    if len(decl) < max(5, lookback // 2):
        return None
    p = _quantile(decl, pctl)
    return max(float(base_floor), float(k) * max(p, 0.0))


def resolve_otm_window(mode: str, closes: Sequence[float] | None, *, flat_floor: float,
                       flat_target: float, flat_cap: float, k: float, lookback: int,
                       pctl: float, horizon: int) -> tuple[float, float, float]:
    """(floor, target, cap) for the put screen.

    "flat" (or no usable closes) → exactly the configured flat window, so the default is
    byte-identical to today's behaviour. "realized" → floor from `realized_otm_floor`;
    the target and cap are never allowed below the floor (target = max(flat_target, floor),
    cap = max(flat_cap, floor + 0.03))."""
    if mode != "realized" or not closes:
        return (flat_floor, flat_target, flat_cap)
    floor = realized_otm_floor(closes, k=k, lookback=lookback, pctl=pctl, horizon=horizon,
                               base_floor=flat_floor)
    if floor is None:
        return (flat_floor, flat_target, flat_cap)
    target = max(flat_target, floor)
    cap = max(flat_cap, floor + 0.03)
    return (round(floor, 4), round(target, 4), round(cap, 4))


def breach_rate(closes: Sequence[float], *, lookback: int, horizon: int,
                threshold: float) -> float | None:
    """Fraction of the trailing `lookback` start days whose forward `horizon`-day decline
    exceeded `threshold`. None when fewer than lookback // 2 samples exist (fail-open)."""
    if not closes:
        return None
    tail = list(closes)[-(lookback + horizon):]
    decl = forward_declines(tail, horizon)
    if len(decl) < max(5, lookback // 2):
        return None
    hits = sum(1 for x in decl if x > threshold)
    return hits / len(decl)
