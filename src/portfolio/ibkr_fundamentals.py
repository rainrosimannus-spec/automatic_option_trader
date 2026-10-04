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
        from src.portfolio.connection import get_portfolio_lock
        with get_portfolio_lock():
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

# ─────────────────────────────────────────────────────────────────────
# Earnings calendar — uses IBKR CalendarReport (same fundamentals subscription
# as ReportsFinSummary). Pure fetch+parse here; caching done by caller in
# src/broker/market_data.py:has_upcoming_earnings().
# ─────────────────────────────────────────────────────────────────────

from typing import NamedTuple
from datetime import date as _date


class EarningsResult(NamedTuple):
    """Three-state result from earnings lookup.

    status='found'           -> next_date is the upcoming earnings date
    status='none_scheduled'  -> CalendarReport parsed cleanly but no
                                EarningsAnnouncement node (e.g. ETF, ADR
                                without coverage). Caller should treat as
                                'no earnings imminent' = allow trade.
    status='fetch_failed'    -> network error, parse error, or no data.
                                Caller should fail-CLOSED = block trade.
    """
    next_date: _date | None
    status: str


def get_next_earnings_date(ib, contract) -> EarningsResult:
    """
    Fetch next earnings date from IBKR CalendarReport.

    Args:
        ib: connected ib_insync IB instance
        contract: qualified Stock contract

    Returns:
        EarningsResult with three explicit states (see class docstring).

    Note: uses get_portfolio_lock() to serialize against other Winston/Maggy
    IBKR calls during merge period. Same pattern as get_ibkr_fundamentals.
    """
    try:
        from src.portfolio.connection import get_portfolio_lock
        with get_portfolio_lock():
            xml = ib.reqFundamentalData(contract, "CalendarReport")
    except Exception as e:
        log.warning("ibkr_calendarreport_fetch_failed",
                    symbol=getattr(contract, "symbol", "?"), error=str(e))
        return EarningsResult(next_date=None, status="fetch_failed")

    if not xml:
        return EarningsResult(next_date=None, status="fetch_failed")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("ibkr_calendarreport_parse_failed",
                    symbol=getattr(contract, "symbol", "?"), error=str(e))
        return EarningsResult(next_date=None, status="fetch_failed")

    # CalendarReport structure (per IBKR docs):
    #   <Calendar><CalendarItems><EarningsAnnouncement Date="2026-05-08" .../>
    # Some payloads use <EPSDates><EPSDate>...</EPSDate></EPSDates> instead.
    # Try both.
    raw = None
    node = root.find(".//EarningsAnnouncement")
    if node is not None:
        raw = node.get("Date") or (node.text or "").strip() or None

    if raw is None:
        # Fallback: <EPSDate> children
        eps_dates = root.findall(".//EPSDate")
        if eps_dates:
            # Find the earliest future date
            today = _date.today()
            future = []
            for n in eps_dates:
                txt = (n.text or "").strip()[:10]
                try:
                    d = _date.fromisoformat(txt)
                    if d >= today:
                        future.append(d)
                except (ValueError, TypeError):
                    continue
            if future:
                return EarningsResult(next_date=min(future), status="found")

    if raw is None:
        # Report parsed cleanly but no earnings node found
        return EarningsResult(next_date=None, status="none_scheduled")

    try:
        earnings_date = _date.fromisoformat(raw[:10])
        return EarningsResult(next_date=earnings_date, status="found")
    except (ValueError, TypeError) as e:
        log.warning("ibkr_calendarreport_date_parse_failed",
                    symbol=getattr(contract, "symbol", "?"), raw=raw, error=str(e))
        return EarningsResult(next_date=None, status="fetch_failed")



# ─────────────────────────────────────────────────────────────────────────────────────────────
# Quality figures from the broker (2026-10-04)
# ─────────────────────────────────────────────────────────────────────────────────────────────
# FMP has no statements for most non-US tickers on our plan, and the ReportsFinSummary fallback
# above carries only revenue, EPS and dividends. So for a foreign name every QUALITY input used
# to be missing and the quality pillar was built entirely from neutral defaults (50.8 — just
# above the 50 floor): such a name was in the growth tier on growth alone.
#
# The account's subscription does not include full statements (ReportsFinStatements returns
# "not available" for every stock), but the analyst-estimates report RESC carries about five
# years of annual ACTUALS for every covered company: revenue, EBIT, pretax income, EPS, return
# on assets and equity, capex, cash flow per share, net debt, gross margin. Those are enough for
# three of the four quality sub-scores. Each derived figure was calibrated against FMP on 70 US
# names that have both sources:
#
#   operating_margin_pct    the LOWER of EBIT/revenue and pretax/revenue. Analysts' EBIT is often
#                           adjusted (it excludes stock compensation and amortisation), which
#                           flatters software and acquirers; taking the lower of the two had the
#                           best agreement (correlation 0.94, median gap +1.1 points).
#   roic_5yr_avg / _min     1.17 x return on assets - 0.45. A direct EBIT/(equity + net debt)
#                           estimate did not track FMP's ROIC at all (correlation -0.17); return
#                           on assets did (0.83 on the average, 0.89 on the minimum), and the
#                           regression puts it on FMP's scale.
#   fcf_margin_trend        (cash flow per share x shares - capex) / revenue, latest year minus
#                           two years earlier (correlation 0.92, median gap 0.2 points).
#
# NOT available from the broker, and left absent (their sub-scores stay neutral): R&D intensity,
# share dilution, goodwill. A name is "quality-measured" only if it has BOTH a return-on-capital
# figure and an operating margin — see growth_gate.passes_entry_gate.
ROA_TO_ROIC_SLOPE = 1.17
ROA_TO_ROIC_INTERCEPT = -0.45
_PERCENT_TYPES = ("ROAPCT", "ROEPCT")


def _resc_actuals(resc_xml: str) -> dict[str, dict[int, float]]:
    """{measure: {fiscal_year: value}} of ANNUAL actuals from a RESC report, in plain units.
    The report mixes scales: unit="M" is millions; the two percentage measures are left RAW here
    because their multiplier varies by market (see _percent_scale)."""
    out: dict[str, dict[int, float]] = {}
    root = ET.fromstring(resc_xml)
    for fy in root.findall("./Actuals/FYActuals/FYActual"):
        measure = fy.get("type") or ""
        years: dict[int, float] = {}
        for period in fy.findall("FYPeriod"):
            if period.get("periodType") != "A":
                continue
            val = period.find("ActValue")
            try:
                x = float(val.text)          # type: ignore[union-attr]
                year = int(period.get("fYear") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if not year:
                continue
            if fy.get("unit") == "M":
                x *= 1e6
            years[year] = x
        if years:
            out[measure] = years
    return out


def _percent_scale(a: dict[str, dict[int, float]]) -> float:
    """Multiplier the report applied to its percentage measures (ROAPCT, ROEPCT).

    They are stored scaled by the reporting unit — a million for most markets (28359000 =
    28.359%), a billion for Japanese companies. The report does not say which, so it is
    recovered from the report itself: return on equity must be close to EPS / book value per
    share, and the ratio of the stored figure to that is the multiplier (rounded to a power of a
    thousand). Without a usable anchor year, a million is assumed."""
    import math
    eps, bvps, roe = a.get("EPS") or {}, a.get("BVPS") or {}, a.get("ROEPCT") or {}
    ratios = []
    for year, raw in roe.items():
        e, b = eps.get(year), bvps.get(year)
        if e is None or not b or b <= 0 or not raw:
            continue
        implied = e / b * 100.0
        if abs(implied) < 0.5 or (raw > 0) != (implied > 0):
            continue                     # too close to zero, or signs disagree: no anchor
        ratios.append(raw / implied)
    if not ratios:
        return 1e6
    ratios.sort()
    power = round(math.log(ratios[len(ratios) // 2], 1000))
    return float(1000 ** min(4, max(0, power)))


def _snapshot_shares(snapshot_xml: str) -> Optional[float]:
    try:
        node = ET.fromstring(snapshot_xml).find("./CoGeneralInfo/SharesOut")
        shares = float(node.text)             # type: ignore[union-attr]
        return shares if shares > 0 else None
    except Exception:
        return None


def parse_quality_fundamentals(resc_xml: Optional[str], snapshot_xml: Optional[str] = None) -> dict:
    """Quality inputs, under the same keys FMP's path produces, from the broker's RESC report
    (plus ReportSnapshot for the share count). Pure: no I/O. Absent data -> absent key."""
    result: dict = {}
    if not resc_xml:
        return result
    try:
        a = _resc_actuals(resc_xml)
    except ET.ParseError:
        return result
    rev = a.get("SREV") or {}
    years = sorted(rev)[-5:]
    if len(years) < 2:
        return result
    latest = years[-1]

    def series(measure: str) -> list[Optional[float]]:
        d = a.get(measure) or {}
        return [d.get(y) for y in years]

    # ── revenue (fallback only; the caller prefers what it already has) ──
    revs = [rev[y] for y in years]
    if revs[0] > 0 and revs[-1] > 0:
        result["revenue_cagr_pct"] = ((revs[-1] / revs[0]) ** (1.0 / (len(revs) - 1)) - 1.0) * 100
    if revs[-2]:
        result["revenue_yoy_pct"] = (revs[-1] - revs[-2]) / abs(revs[-2]) * 100

    # ── operating margin: the lower of EBIT margin and pretax margin, level and 3-year trend ──
    ebit, pretax = series("EBIT"), series("EIBT")
    margins: list[Optional[float]] = []
    for e, p, r in zip(ebit, pretax, revs):
        cands = [x / r * 100 for x in (e, p) if x is not None and r]
        margins.append(min(cands) if cands else None)
    if margins[-1] is not None:
        result["operating_margin_pct"] = margins[-1]
        if len(margins) >= 3 and margins[-3] is not None:
            result["operating_margin_trend"] = margins[-1] - margins[-3]

    # ── return on capital: return on assets, put on FMP's ROIC scale ──
    pct_scale = _percent_scale(a)
    roa = [x / pct_scale for x in series("ROAPCT") if x is not None]
    # A return on assets outside +/-100% means the scale could not be trusted: leave the figure
    # ABSENT (the name then counts as quality-unmeasured) rather than score a nonsense number.
    if roa and all(abs(x) <= 100 for x in roa):
        scaled = [ROA_TO_ROIC_SLOPE * x + ROA_TO_ROIC_INTERCEPT for x in roa]
        result["roic_5yr_avg"] = sum(scaled) / len(scaled)
        result["roic_5yr_min"] = min(scaled)
        result["compounding_quality_raw"] = result["roic_5yr_avg"]

    # ── gross margin ──
    gm = series("GROSMGN")
    if gm[-1] is not None:
        result["gross_margin_pct"] = gm[-1]
        if len(gm) >= 3 and gm[-3] is not None:
            result["gross_margin_trend"] = gm[-1] - gm[-3]

    # ── free cash flow margin: trend and count of negative years ──
    shares = _snapshot_shares(snapshot_xml) if snapshot_xml else None
    cfps, capex = series("CFSHR"), series("SCEX")
    if shares:
        fcf_margin: list[Optional[float]] = []
        for c, x, r in zip(cfps, capex, revs):
            fcf_margin.append(((c * shares) - abs(x or 0.0)) / r * 100 if c is not None and r else None)
        if fcf_margin[-1] is not None and len(fcf_margin) >= 3 and fcf_margin[-3] is not None:
            result["fcf_margin_trend"] = fcf_margin[-1] - fcf_margin[-3]
        known = [m for m in fcf_margin if m is not None]
        if known:
            result["fcf_negative_years_5yr"] = sum(1 for m in known if m < 0)

    # ── loss years (GAAP EPS where the report has it) ──
    eps = [x for x in (series("GPS") if a.get("GPS") else series("EPS")) if x is not None]
    if eps:
        result["net_income_negative_years_5yr"] = sum(1 for x in eps if x < 0)

    result["quality_source"] = "ibkr_resc"
    result["quality_fiscal_year"] = latest
    return result


def get_ibkr_quality_fundamentals(ib, contract) -> dict:
    """Fetch RESC (+ ReportSnapshot for the share count) and return the quality inputs.
    {} when the company has no analyst coverage or the request fails — never a default."""
    try:
        from src.portfolio.connection import get_portfolio_lock
        with get_portfolio_lock():
            resc = ib.reqFundamentalData(contract, "RESC")
            snapshot = ib.reqFundamentalData(contract, "ReportSnapshot") if resc else None
    except Exception as e:
        log.warning("ibkr_quality_fundamentals_failed", symbol=contract.symbol, error=str(e))
        return {}
    try:
        return parse_quality_fundamentals(resc, snapshot)
    except Exception as e:
        log.warning("ibkr_quality_fundamentals_parse_failed", symbol=contract.symbol, error=str(e))
        return {}
