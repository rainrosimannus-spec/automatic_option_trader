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


def friday_expiries(today: date, max_days: int = 55) -> list[str]:
    """Weekly Friday expiries (YYYYMMDD) from `today` out to max_days ahead."""
    out = []
    d = today + timedelta(days=1)
    end = today + timedelta(days=max_days)
    while d <= end:
        if d.weekday() == 4:  # Friday
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def build_contracts(spot: float, today: date, max_days: int = 55) -> list[SynthContract]:
    """Full synthetic chain: every strike × every Friday expiry in range."""
    strikes = strike_grid(spot)
    expiries = friday_expiries(today, max_days)
    return [SynthContract(k, e) for e in expiries for k in strikes]


def _dte(expiry: str, today: date) -> int:
    return (datetime.strptime(expiry, "%Y%m%d").date() - today).days


# Short-dated vol-premium uplift. Flat-IV BSM prices near-expiry OTM options at
# pure-theoretical pennies, but real markets price them richer (vol term
# structure + bid floor / min tick), which is why the 0-3 DTE cash machine
# backtests near-zero otherwise. Below T0 days, scale IV up toward realistic
# short-dated levels. TUNABLE — these are the knobs.
SHORT_DTE_T0 = 7        # apply the uplift below this DTE
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
