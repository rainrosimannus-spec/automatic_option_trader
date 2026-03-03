"""
Black-Scholes greeks calculator — subscription-free option pricing.

Computes delta, gamma, theta, vega, and theoretical prices for puts and calls
using only historical data (stock price + implied volatility from reqHistoricalData).
No streaming market data subscriptions required.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.core.logger import get_logger

log = get_logger(__name__)

RISK_FREE_RATE = 0.045  # ~current US 10-year yield


def _norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation via erf)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class OptionGreeks:
    """Computed greeks and pricing for a single option."""
    delta: float
    gamma: float
    theta: float  # daily theta
    vega: float  # per 1% IV move
    iv: float
    theo_price: float
    bid: float
    ask: float
    mid: float


def _compute_d1_d2(S: float, K: float, T: float, r: float, sigma: float):
    """Compute d1 and d2 for Black-Scholes."""
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes European put price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, d2 = _compute_d1_d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes European call price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, d2 = _compute_d1_d2(S, K, T, r, sigma)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def compute_put_greeks(
    S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE,
    spread_pct: float = 0.05,
) -> Optional[OptionGreeks]:
    """
    Compute full greeks for a put option.

    Args:
        S: stock price
        K: strike price
        T: time to expiry in years
        sigma: implied volatility (annualized)
        r: risk-free rate
        spread_pct: bid-ask spread as fraction of theo price (default 5%)

    Returns:
        OptionGreeks with all computed values, or None if inputs invalid.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None

    d1, d2 = _compute_d1_d2(S, K, T, r, sigma)
    sqrt_T = math.sqrt(T)

    # Price
    theo = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

    # Delta (put delta is negative, we store absolute value for screening)
    delta = _norm_cdf(d1) - 1.0  # negative for puts

    # Gamma
    gamma = _norm_pdf(d1) / (S * sigma * sqrt_T)

    # Theta (per calendar day)
    theta_annual = (
        -(S * _norm_pdf(d1) * sigma) / (2 * sqrt_T)
        + r * K * math.exp(-r * T) * _norm_cdf(-d2)
    )
    theta = theta_annual / 365.0

    # Vega (per 1% IV move)
    vega = S * _norm_pdf(d1) * sqrt_T / 100.0

    # Synthetic bid/ask with spread
    half_spread = max(theo * spread_pct / 2, 0.01)
    bid = max(round(theo - half_spread, 2), 0.01)
    ask = round(theo + half_spread, 2)
    mid = round(theo, 2)

    return OptionGreeks(
        delta=delta,
        gamma=round(gamma, 6),
        theta=round(theta, 4),
        vega=round(vega, 4),
        iv=sigma,
        theo_price=round(theo, 4),
        bid=bid,
        ask=ask,
        mid=mid,
    )


def compute_call_greeks(
    S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE,
    spread_pct: float = 0.05,
) -> Optional[OptionGreeks]:
    """Compute full greeks for a call option."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None

    d1, d2 = _compute_d1_d2(S, K, T, r, sigma)
    sqrt_T = math.sqrt(T)

    theo = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    delta = _norm_cdf(d1)
    gamma = _norm_pdf(d1) / (S * sigma * sqrt_T)
    theta_annual = (
        -(S * _norm_pdf(d1) * sigma) / (2 * sqrt_T)
        - r * K * math.exp(-r * T) * _norm_cdf(d2)
    )
    theta = theta_annual / 365.0
    vega = S * _norm_pdf(d1) * sqrt_T / 100.0

    half_spread = max(theo * spread_pct / 2, 0.01)
    bid = max(round(theo - half_spread, 2), 0.01)
    ask = round(theo + half_spread, 2)
    mid = round(theo, 2)

    return OptionGreeks(
        delta=delta,
        gamma=round(gamma, 6),
        theta=round(theta, 4),
        vega=round(vega, 4),
        iv=sigma,
        theo_price=round(theo, 4),
        bid=bid,
        ask=ask,
        mid=mid,
    )


def get_current_iv(
    ib, symbol: str, exchange: str = "SMART", currency: str = "USD",
) -> Optional[float]:
    """
    Get current implied volatility for a stock using historical data.
    Uses reqHistoricalData with OPTION_IMPLIED_VOLATILITY — no subscription needed.

    Returns annualized IV or None.
    """
    from src.broker.market_data import _make_stock_contract

    try:
        contract = _make_stock_contract(symbol, exchange, currency)
        ib.qualifyContracts(contract)
        contract.exchange = "SMART"

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="5 D",
            barSizeSetting="1 day",
            whatToShow="OPTION_IMPLIED_VOLATILITY",
            useRTH=False,
            formatDate=1,
            timeout=10,
        )

        if bars:
            # Use the most recent IV value
            for bar in reversed(bars):
                if bar.close and bar.close > 0:
                    return float(bar.close)

        return None
    except Exception as e:
        log.debug("iv_fetch_error", symbol=symbol, error=str(e))
        return None
