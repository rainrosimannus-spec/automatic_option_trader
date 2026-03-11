"""
Financial Modeling Prep (FMP) API client.
Provides fundamental data: revenue growth, dividends, payout ratio, debt metrics.
Free tier: 250 requests/day — use sparingly (monthly screener only).
"""
from __future__ import annotations

import requests
from typing import Optional
from src.core.logger import get_logger

log = get_logger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"


def get_fmp_key() -> Optional[str]:
    """Get FMP API key from settings.yaml."""
    try:
        from src.core.config import get_settings
        key = get_settings().raw.get("fmp", {}).get("api_key")
        if key:
            return key
    except Exception:
        pass
    import os
    return os.environ.get("FMP_API_KEY")


def _get(endpoint: str, symbol: str, params: dict = {}) -> Optional[dict | list]:
    """Make a GET request to FMP API."""
    key = get_fmp_key()
    if not key:
        log.error("fmp_api_key_missing")
        return None
    try:
        all_params = {"symbol": symbol, "apikey": key}
        all_params.update(params)
        resp = requests.get(f"{FMP_BASE}/{endpoint}", params=all_params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("fmp_request_failed", endpoint=endpoint, error=str(e))
        return None


def get_income_growth(symbol: str, years: int = 3) -> Optional[dict]:
    """
    Get revenue and earnings growth over last N years.
    Returns: {revenue_growth_yoy: float, avg_revenue_growth: float}
    """
    data = _get("income-statement", symbol, {"limit": years + 1, "period": "annual"})
    if not data or len(data) < 2:
        return None
    try:
        latest = data[0]["revenue"]
        prior = data[1]["revenue"]
        oldest = data[-1]["revenue"]
        yoy = (latest - prior) / abs(prior) * 100 if prior else 0
        avg = (latest - oldest) / abs(oldest) * 100 / years if oldest and years > 0 else 0
        return {
            "revenue_latest": latest,
            "revenue_yoy_pct": round(yoy, 1),
            "revenue_avg_annual_pct": round(avg, 1),
        }
    except Exception as e:
        log.warning("fmp_income_parse_failed", symbol=symbol, error=str(e))
        return None


def get_dividend_data(symbol: str) -> Optional[dict]:
    """
    Get dividend metrics: yield, payout ratio, growth, cuts.
    Returns: {dividend_yield, payout_ratio, dividend_growth_yoy, dividend_cut}
    """
    data = _get("ratios", symbol, {"limit": 3, "period": "annual"})
    if not data or len(data) < 1:
        return None
    try:
        latest = data[0]
        result = {
            "dividend_yield": round((latest.get("dividendYield") or 0) * 100, 2),
            "payout_ratio": round((latest.get("payoutRatio") or 0) * 100, 1),
            "dividend_cut": False,
        }
        # Check for dividend cut (yield dropped significantly year over year)
        if len(data) >= 2:
            prev_yield = (data[1].get("dividendYield") or 0) * 100
            curr_yield = result["dividend_yield"]
            if prev_yield > 0.5 and curr_yield < prev_yield * 0.7:
                result["dividend_cut"] = True
        return result
    except Exception as e:
        log.warning("fmp_dividend_parse_failed", symbol=symbol, error=str(e))
        return None


def get_debt_metrics(symbol: str) -> Optional[dict]:
    """
    Get debt/equity ratio and free cash flow.
    Returns: {debt_to_equity, fcf_positive, fcf_negative_years}
    """
    data = _get("balance-sheet-statement", symbol, {"limit": 3, "period": "annual"})
    cf_data = _get("cash-flow-statement", symbol, {"limit": 3, "period": "annual"})
    if not data:
        return None
    try:
        latest = data[0]
        total_debt = latest.get("totalDebt") or 0
        equity = latest.get("totalStockholdersEquity") or 1
        de_ratio = total_debt / abs(equity) if equity else 0

        fcf_negative_years = 0
        if cf_data:
            for cf in cf_data:
                fcf = (cf.get("freeCashFlow") or 0)
                if fcf < 0:
                    fcf_negative_years += 1

        return {
            "debt_to_equity": round(de_ratio, 2),
            "fcf_negative_years": fcf_negative_years,
        }
    except Exception as e:
        log.warning("fmp_debt_parse_failed", symbol=symbol, error=str(e))
        return None


def get_full_fundamentals(symbol: str) -> Optional[dict]:
    """
    Fetch all fundamental metrics needed for screening in one call bundle.
    Returns combined dict or None if data unavailable.
    Uses 3 API calls — use sparingly.
    """
    income = get_income_growth(symbol)
    dividends = get_dividend_data(symbol)
    debt = get_debt_metrics(symbol)

    if not any([income, dividends, debt]):
        return None

    result = {}
    if income:
        result.update(income)
    if dividends:
        result.update(dividends)
    if debt:
        result.update(debt)

    log.info("fmp_fundamentals_fetched", symbol=symbol, metrics=list(result.keys()))
    return result
