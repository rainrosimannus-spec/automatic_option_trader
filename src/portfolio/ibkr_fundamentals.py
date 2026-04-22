"""
IBKR fundamentals fallback for stocks FMP cannot resolve.

Parses IBKR's ReportsFinSummary XML to extract fundamental data for
non-US primary listings (LSE, AEB, HKEX, BM, etc.) where FMP returns
nothing.

Returns a dict with same keys as FMP's _get_fmp_fundamentals. Each field
is either present with a real value or ABSENT. Never silently defaulted.
Callers can tell the difference between "data missing" and "value is 0".

Usage:
    from src.portfolio.ibkr_fundamentals import get_ibkr_fundamentals
    data = get_ibkr_fundamentals(ib, contract, current_price=12.34)
    # data = {"dividend_yield": 6.1, "payout_ratio": 0.75, ...}  or {}
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from src.core.logger import get_logger

log = get_logger(__name__)


def _parse_dps_history(root: ET.Element) -> list[tuple[str, float]]:
    """
    Extract (asofDate, value) tuples for DividendPerShare with reportType='R' (reported)
    and period='12M'. Returns sorted list, oldest first.
    """
    out = []
    for dps in root.findall(".//DividendPerShares/DividendPerShare"):
        report_type = dps.get("reportType")
        period = dps.get("period")
        if report_type != "R" or period != "12M":
            continue
        date = dps.get("asofDate")
        try:
            value = float(dps.text) if dps.text else None
        except (ValueError, TypeError):
            value = None
        if date and value is not None:
            out.append((date, value))
    out.sort(key=lambda x: x[0])
    return out


def _parse_eps_history(root: ET.Element) -> list[tuple[str, float]]:
    """Extract (asofDate, value) for EPS reportType='R' period='12M', sorted oldest first."""
    out = []
    for eps in root.findall(".//EPSs/EPS"):
        if eps.get("reportType") != "R" or eps.get("period") != "12M":
            continue
        date = eps.get("asofDate")
        try:
            value = float(eps.text) if eps.text else None
        except (ValueError, TypeError):
            value = None
        if date and value is not None:
            out.append((date, value))
    out.sort(key=lambda x: x[0])
    return out


def _parse_revenue_history(root: ET.Element) -> list[tuple[str, float]]:
    """Extract (asofDate, value) for TotalRevenue reportType='A' period='12M', sorted oldest first."""
    out = []
    for rev in root.findall(".//TotalRevenues/TotalRevenue"):
        if rev.get("reportType") != "A" or rev.get("period") != "12M":
            continue
        date = rev.get("asofDate")
        try:
            value = float(rev.text) if rev.text else None
        except (ValueError, TypeError):
            value = None
        if date and value is not None:
            out.append((date, value))
    out.sort(key=lambda x: x[0])
    return out


def _annual_dps(dps_history: list[tuple[str, float]]) -> dict[int, float]:
    """
    Reduce DPS history to ONE value per calendar year — take the latest asofDate for each year.
    Returns {year: dps_value}.
    """
    by_year: dict[int, tuple[str, float]] = {}
    for date, value in dps_history:
        year = int(date[:4])
        if year not in by_year or date > by_year[year][0]:
            by_year[year] = (date, value)
    return {y: v for y, (_, v) in by_year.items()}


def _cagr(start: float, end: float, years: int) -> Optional[float]:
    """Compound annual growth rate, as a percentage. Returns None if invalid inputs."""
    if start <= 0 or end <= 0 or years <= 0:
        return None
    return (((end / start) ** (1.0 / years)) - 1.0) * 100


def get_ibkr_fundamentals(ib, contract, current_price: Optional[float] = None) -> dict:
    """
    Fetch and parse IBKR fundamentals for a contract.
    Returns a dict with ONLY the fields we can honestly compute — missing fields absent.

    Args:
        ib: connected ib_insync IB instance
        contract: qualified Stock contract
        current_price: optional current price for yield calculation
    """
    result: dict = {}

    # ReportsFinSummary — dividend history, EPS, revenue
    try:
        xml = ib.reqFundamentalData(contract, "ReportsFinSummary")
    except Exception as e:
        log.warning("ibkr_fundamentals_finsummary_failed", symbol=contract.symbol, error=str(e))
        return result

    if not xml:
        return result

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("ibkr_fundamentals_parse_failed", symbol=contract.symbol, error=str(e))
        return result

    # ── Dividend per share history ──
    dps_history = _parse_dps_history(root)
    dps_by_year = _annual_dps(dps_history)

    if dps_by_year:
        years_sorted = sorted(dps_by_year.keys())
        latest_year = years_sorted[-1]
        latest_dps = dps_by_year[latest_year]

        # Dividend yield = latest annual DPS / current price
        if current_price and current_price > 0:
            result["dividend_yield"] = (latest_dps / current_price) * 100

        # 3yr CAGR (latest vs 3 years ago)
        target_3y = latest_year - 3
        if target_3y in dps_by_year:
            cagr3 = _cagr(dps_by_year[target_3y], latest_dps, 3)
            if cagr3 is not None:
                result["dividend_cagr_3yr"] = cagr3

        # 5yr CAGR
        target_5y = latest_year - 5
        if target_5y in dps_by_year:
            cagr5 = _cagr(dps_by_year[target_5y], latest_dps, 5)
            if cagr5 is not None:
                result["dividend_cagr_5yr"] = cagr5

        # Real dividend cut detection — year-over-year drop of >20% in annual DPS,
        # but ONLY for cuts in the last 2 year transitions. A cut 5 years ago with
        # strong recovery since is not a structural disqualifier.
        if len(years_sorted) >= 2:
            cut_detected = False
            # Examine only the most recent 2 year transitions (i.e. last 3 years)
            recent_years = years_sorted[-3:] if len(years_sorted) >= 3 else years_sorted
            for i in range(1, len(recent_years)):
                prev_year = recent_years[i - 1]
                curr_year = recent_years[i]
                if curr_year - prev_year != 1:
                    continue
                prev_dps = dps_by_year[prev_year]
                curr_dps = dps_by_year[curr_year]
                if prev_dps > 0 and curr_dps < prev_dps * 0.80:
                    cut_detected = True
                    break
            result["dividend_cut"] = cut_detected

    # ── EPS history — for payout ratio ──
    eps_history = _parse_eps_history(root)
    if dps_by_year and eps_history:
        # Use most recent EPS matched to same year as latest DPS
        eps_by_year: dict[int, float] = {}
        for date, value in eps_history:
            year = int(date[:4])
            if year not in eps_by_year or date > eps_history[0][0]:
                eps_by_year[year] = value

        latest_year = max(dps_by_year.keys())
        if latest_year in eps_by_year and eps_by_year[latest_year] > 0:
            payout = dps_by_year[latest_year] / eps_by_year[latest_year]
            # Only report payout if it's a sensible value (0.0 to 2.0, i.e. 0% to 200%)
            if 0 <= payout <= 2.0:
                result["payout_ratio"] = payout * 100  # match FMP percentage scale

    # ── Revenue history — for growth metrics ──
    revenue_history = _parse_revenue_history(root)
    if len(revenue_history) >= 2:
        # Find latest annual and prior-year annual
        by_year_rev: dict[int, float] = {}
        for date, value in revenue_history:
            year = int(date[:4])
            if year not in by_year_rev:
                by_year_rev[year] = value
        rev_years = sorted(by_year_rev.keys())
        if len(rev_years) >= 2:
            latest_rev = by_year_rev[rev_years[-1]]
            prior_rev = by_year_rev[rev_years[-2]]
            if prior_rev > 0:
                result["revenue_yoy_pct"] = ((latest_rev / prior_rev) - 1.0) * 100

        # 5yr average
        if len(rev_years) >= 6:
            five_yrs_ago = by_year_rev[rev_years[-6]]
            latest = by_year_rev[rev_years[-1]]
            if five_yrs_ago > 0:
                avg_cagr = _cagr(five_yrs_ago, latest, 5)
                if avg_cagr is not None:
                    result["revenue_avg_pct"] = avg_cagr

    return result
