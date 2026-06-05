"""
Synthetic option-chain construction + BSM valuation for the sandbox.

No IBKR. Strikes/expiries are generated to mimic listed weekly options; pricing
uses the SAME compute_put_greeks / compute_call_greeks the live system uses, so
the backtest's marks are consistent with production's pricing model.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.broker.greeks import compute_put_greeks, compute_call_greeks


class SynthContract:
    """Minimal stand-in for an IBKR option contract.

    The shared scoring cores only read `.strike` and
    `.lastTradeDateOrContractMonth`, so that's all we provide.
    """
    __slots__ = ("strike", "lastTradeDateOrContractMonth")

    def __init__(self, strike: float, expiry: str):
        self.strike = strike
        self.lastTradeDateOrContractMonth = expiry


def strike_increment(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 200:
        return 2.5
    return 5.0


def strike_grid(spot: float, lo_pct: float = 0.70, hi_pct: float = 1.30) -> list[float]:
    """Listed-style strikes spanning lo_pct..hi_pct of spot."""
    inc = strike_increment(spot)
    lo = inc * round((spot * lo_pct) / inc)
    hi = inc * round((spot * hi_pct) / inc)
    strikes = []
    k = lo
    while k <= hi + 1e-9:
        if k > 0:
            strikes.append(round(k, 2))
        k += inc
    return strikes


# Mega-cap tech with at least Mon+Wed+Fri weekly expiries since ~2022. Daily
# 0DTE listings (full M/T/W/Th/F) on individual equities only became common
# mid-2023; using MWF across the backtest era is the closest single
# approximation. Names outside this set are Friday-only — that matches real
# market structure where most weeklies anchor on Friday and is why "dead days"
# (Mon for most of the universe, Fri for everyone) remain part of the
# simulation. ASX names would expire Thursday but the marswalk universe is
# US-only (regimes.yaml drops 3690/ABF/XRO), so we don't emit Thursdays.
MWF_EXPIRY_SYMBOLS = frozenset({
    "AAPL", "MSFT", "GOOG", "META", "AMZN", "NVDA", "TSLA", "AMD", "AVGO",
})


def expiries_for(today: date, max_days: int, symbol: str | None = None) -> list[str]:
    """Synthetic listed-style expiries (YYYYMMDD) from `today` out to
    `max_days` ahead. Friday-only for most symbols; Mon+Wed+Fri for the
    mega-cap tech set (see `MWF_EXPIRY_SYMBOLS`). `symbol=None` keeps the
    legacy Friday-only behavior for any callers we haven't updated."""
    allowed = {4}  # Friday default
    if symbol and symbol.upper() in MWF_EXPIRY_SYMBOLS:
        allowed = {0, 2, 4}  # Mon, Wed, Fri
    out = []
    d = today + timedelta(days=1)
    end = today + timedelta(days=max_days)
    while d <= end:
        if d.weekday() in allowed:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


# Back-compat alias — some external callers/tests may still import the old name.
def friday_expiries(today: date, max_days: int = 55) -> list[str]:
    return expiries_for(today, max_days, symbol=None)


def build_contracts(spot: float, today: date, max_days: int = 55,
                    symbol: str | None = None) -> list[SynthContract]:
    """Full synthetic chain: every strike × every available expiry in range.
    Pass `symbol` so the expiry set respects per-name market structure
    (Friday-only vs Mon+Wed+Fri for mega-cap tech)."""
    strikes = strike_grid(spot)
    expiries = expiries_for(today, max_days, symbol=symbol)
    return [SynthContract(k, e) for e in expiries for k in strikes]


def _dte(expiry: str, today: date) -> int:
    return (datetime.strptime(expiry, "%Y%m%d").date() - today).days


# Short-dated vol-premium uplift. Flat-IV BSM prices near-expiry OTM options at
# pure-theoretical pennies, but real markets price them richer (vol term
# structure + bid floor / min tick), which is why the 0-3 DTE cash machine
# backtests near-zero otherwise. Below T0 days, scale IV up toward realistic
# short-dated levels. TUNABLE — these are the knobs.
SHORT_DTE_T0 = 7        # apply the uplift below this DTE
# Lowered 2026-05-26 from 4.95 -> 1.0 after the BSM-vs-market measurement
# (commit 12341e0 / pricing_calibration_20260526.jsonl): the k=4.95 fit was
# anchored to a single regime's outcome (iran_war +22.5% son's live) while
# the gate stack was choked, so the value compensated for under-deployment
# as well as pricing. With growth-mode (2e944cf) removing the deployment
# choke, the empirical measurement showed BSM is within ~13% of real-market
# mid for 0-7 DTE OTM puts on average — k=4.95 was multiplying that gap by
# ~6x. k=1.0 means 2x IV at DTE=0, 1.57x at DTE=3, 1x at DTE=7: a moderate
# acknowledgement of vol-smile + bid-floor amplification without the fudge.
# MarsWalk magnitudes are now relative not absolute; anchor live P&L against
# actual fills, not against backtested numbers.
SHORT_DTE_K = 1.0       # max multiplicative uplift at 0 DTE (iv *= 1 + K)


def effective_iv(iv: float, dte: int, k: float = SHORT_DTE_K) -> float:
    """IV with the short-dated vol-premium uplift applied (dte in days)."""
    if dte < 0 or dte >= SHORT_DTE_T0 or iv <= 0:
        return iv
    return iv * (1 + k * (SHORT_DTE_T0 - dte) / SHORT_DTE_T0)


def value_put(spot: float, strike: float, expiry: str, today: date, iv: float,
              k: float = SHORT_DTE_K) -> float:
    """BSM mid value of a put (the daily mark). 0 if expired/unpriceable."""
    dte = _dte(expiry, today)
    if dte < 0:
        return max(0.0, strike - spot)  # intrinsic at/after expiry
    T = max(dte, 0.25) / 365.0
    g = compute_put_greeks(spot, strike, T, effective_iv(iv, dte, k))
    return float(g.mid) if g else 0.0


def value_call(spot: float, strike: float, expiry: str, today: date, iv: float,
               k: float = SHORT_DTE_K) -> float:
    """BSM mid value of a call (the daily mark). 0 if expired/unpriceable."""
    dte = _dte(expiry, today)
    if dte < 0:
        return max(0.0, spot - strike)  # intrinsic at/after expiry
    T = max(dte, 0.25) / 365.0
    g = compute_call_greeks(spot, strike, T, effective_iv(iv, dte, k))
    return float(g.mid) if g else 0.0
