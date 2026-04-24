"""
Watchlist Screener — Monthly Universe Refresh Tool

Screens global stocks across three tiers:
  - Breakthrough (25): Early-stage high-potential companies via AI scan
  - Growth (60): High revenue growth, strong fundamentals globally
  - Dividend (15): Best 10-year total return forecast (price + yield)

Outputs:
  - config/screened_universe.yaml  — full 100-stock portfolio watchlist
  - config/options_universe.yaml   — top 50 re-ranked with options liquidity
                                     (used by options trader, not portfolio)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, Option

# ── Dedicated dividend candidate pool ─────────────────────────────────────────
# These are screened in addition to regional pools.
# Focus: long dividend history, growing payouts, strong FCF, not value traps.
# Scored by _score_dividend_total_return() — best 15 selected for dividend tier.
DIVIDEND_CANDIDATES = {
    "US_DIV": {
        "exchange": "SMART", "currency": "USD",
        "symbols": [
            # Telecom — high yield, stable cash flows
            "T", "VZ",
            # Tobacco — controversial but exceptional dividend history
            "MO", "PM",
            # REITs — income vehicles
            "O", "AMT", "PLD", "SPG",
            # US dividend growers — consistent CAGR
            "TXN", "CSCO", "IBM", "WFC", "BAC", "USB",
            "PRU", "AFL", "MET", "MMM", "EMR", "ETN",
            "NEE", "D", "SO", "DUK", "AEP",  # utilities
            "CVS", "WBA",  # pharmacy
            "NLY", "MAIN", "ARCC",  # BDCs/mREITs
            "EPD", "ET", "MMP",  # midstream energy
            "XOM", "CVX",  # already in US but strong dividend
        ],
    },
    "NO_DIV": {
        "exchange": "OSE", "currency": "NOK",
        "symbols": [
            "BWLPG", "HAUTO", "EQNR", "MOWI", "AKRBP",
            "DNB", "ORK", "YAR", "SUBC", "SFL",
        ],
    },
    "UK_DIV": {
        "exchange": "LSE", "currency": "GBP",
        "symbols": [
            "SHEL", "BP", "BATS", "IMB", "LGEN", "AVST",
            "NG", "SSE", "WPP", "MNG", "HWDN",
        ],
    },
    "EU_DIV": {
        "exchange": "AEB", "currency": "EUR",
        "symbols": [
            "PHIA", "UNA", "RAND", "ABN",  # Netherlands
        ],
    },
    "ES_DIV": {
        "exchange": "BM", "currency": "EUR",
        "symbols": ["TEF", "ENG", "IBE", "REP"],
    },
    "ADR_DIV": {
        # Emerging market dividend payers via US ADR — USD, SMART exchange
        "exchange": "SMART", "currency": "USD",
        "symbols": [
            "PBR", "EC", "HDB", "IBN", "SFL",
            "VALE", "RIO", "BHP",  # miners with variable dividends
        ],
    },
}

CANDIDATE_POOLS = {
    "US": {
        "exchange": "SMART",
        "currency": "USD",
        "symbols": [
            "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
            "CRM", "AMD", "NFLX", "ADBE", "NOW", "UBER", "PLTR", "PANW",
            "CRWD", "SHOP", "COIN", "MELI", "ANET", "DDOG", "TTD", "NET",
            "ARM", "SNOW", "ABNB", "SQ", "RIVN", "SOFI", "RBLX", "DASH",
            "ORCL", "INTC", "QCOM", "MU", "LRCX", "KLAC", "CDNS", "SNPS",
            "LLY", "UNH", "ABBV", "JNJ", "MRK", "PFE", "TMO", "ABT",
            "ISRG", "VRTX", "REGN", "DXCM", "MRNA", "GILD", "AMGN",
            "JPM", "V", "MA", "GS", "BLK", "SCHW", "AXP", "C", "MS",
            "COST", "WMT", "HD", "NKE", "SBUX", "MCD", "PG", "KO", "PEP",
            "TGT", "LOW", "BKNG",
            "CAT", "DE", "GE", "RTX", "LMT", "UNP", "HON", "BA",
            "XOM", "CVX", "SLB", "OXY", "COP", "EOG",
        ],
    },
    "CA": {
        "exchange": "SMART", "currency": "CAD", "primary_exchange": "TSE",
        "symbols": [
            "SHOP", "RY", "TD", "ENB", "CNR", "CP", "BMO", "BNS",
            "SU", "TRP", "BCE", "MFC", "ATD", "CNQ", "WCN", "CSU",
            "BAM", "FTS", "QSR", "LSPD",
        ],
    },
    "UK": {
        "exchange": "LSE", "currency": "GBP",
        "symbols": [
            "SHEL", "AZN", "ULVR", "HSBA", "BP", "GSK", "RIO", "LSEG",
            "REL", "DGE", "BATS", "ABF", "PRU", "LLOY", "BARC",
            "VOD", "NG", "SSE", "AAL", "GLEN", "EXPN",
            "CPG", "IMB", "TSCO", "ANTO", "RKT", "CRH", "SMIN",
        ],
    },
    "DE": {
        "exchange": "IBIS", "currency": "EUR",
        "symbols": [
            "SAP", "SIE", "ALV", "MUV2", "DTE", "BAS", "BAYN", "BMW",
            "MBG", "ADS", "IFX", "DBK", "HEN3", "MRK", "FRE",
            "VOW3", "RHM", "SHL", "DHL", "AIR", "MTX", "QIA",
        ],
    },
    "FR": {
        "exchange": "SBF", "currency": "EUR",
        "symbols": [
            "MC", "OR", "TTE", "SAN", "AI", "SU", "BN", "CS",
            "AIR", "SAF", "RI", "KER", "DSY", "CAP", "HO",
            "SGO", "DG", "RMS", "STM", "ACA", "BNP",
        ],
    },
    "NL": {
        "exchange": "AEB", "currency": "EUR",
        "symbols": [
            "ASML", "INGA", "PHIA", "AD", "WKL", "UNA", "HEIA",
            "AKZA", "ASM", "BESI", "PRX", "REN", "ABN", "RAND",
        ],
    },
    "CH": {
        "exchange": "SWX", "currency": "CHF",
        "symbols": [
            "NESN", "NOVN", "ROG", "SIKA", "LONN", "GIVN", "GEBN",
            "UBSG", "ZURN", "SREN", "ABBN", "SLHN",
            "PGHN", "TEMN", "VACN", "LOGN", "AMS", "BARN", "SCMN",
        ],
    },
    "BE": {
        "exchange": "ENEXT.BE", "currency": "EUR",
        "symbols": ["ABI", "UCB", "KBC", "SOLB", "ACKB", "AGS", "COLR"],
    },
    "IE": {
        "exchange": "ISE", "currency": "EUR",
        "symbols": ["CRH", "RYA", "KRX", "SKG", "FLT"],
    },
    "ES": {
        "exchange": "BM", "currency": "EUR",
        "symbols": [
            "SAN", "BBVA", "ITX", "IBE", "TEF", "FER",
            "REP", "AENA", "GRF", "CLNX", "ENG",
        ],
    },
    "IT": {
        "exchange": "BVME", "currency": "EUR",
        "symbols": ["ENEL", "ISP", "UCG", "ENI", "STM", "RACE", "CNHI", "TEN", "AMP", "MONC"],
    },
    "AT": {
        "exchange": "VSE", "currency": "EUR",
        "symbols": ["VOE", "OMV", "EBS", "VER", "RBI"],
    },
    "SE": {
        "exchange": "SFB", "currency": "SEK",
        "symbols": [
            "ATCO-A", "INVE-B", "VOLV-B", "SAND", "ERIC-B", "HEXA-B",
            "ASSA-B", "ALFA", "SEB-A", "SWED-A", "SHB-A", "ESSITY-B",
            "EVO", "SINCH", "HMS",
        ],
    },
    "DK": {
        "exchange": "CSE", "currency": "DKK",
        "symbols": [
            "NOVO-B", "MAERSK-B", "DSV", "VWS", "CARL-B", "COLO-B",
            "NZYM-B", "ORSTED", "PNDORA", "GN", "DEMANT", "FLS",
        ],
    },
    "FI": {
        "exchange": "HEX", "currency": "EUR",
        "symbols": ["NOKIA", "SAMPO", "NESTE", "UPM", "FORTUM", "KNEBV", "STERV", "ELISA"],
    },
    "NO": {
        "exchange": "OSE", "currency": "NOK",
        "symbols": [
            "EQNR", "MOWI", "DNB", "TEL", "ORK", "SALM", "YAR",
            "AKRBP", "SUBC", "AKER", "SCHA", "BAKKA",
        ],
    },
    "JP": {
        "exchange": "TSEJ", "currency": "JPY", "contract_size": 100,
        "symbols": [
            "6758", "6861", "7203", "6367", "8306", "9984", "6902",
            "7741", "4063", "6501", "7267", "8035", "9983", "4502",
            "6098", "3382", "2802", "6273", "7974", "4661",
            "6594", "6723", "6752", "8001", "8766", "9432", "9433",
        ],
    },
    "HK": {
        "exchange": "SEHK", "currency": "HKD",
        "symbols": [
            "0700", "9988", "0005", "0941", "2318", "0388", "1299",
            "0883", "2269", "1211", "0001", "0016", "0002", "0066",
            "1810", "0669", "3690", "9618", "0175", "1928",
        ],
    },
    "SG": {
        "exchange": "SGX", "currency": "SGD",
        "symbols": ["D05", "O39", "U11", "Z74", "BN4", "C6L", "C38U", "A17U", "G13", "S58", "F34", "S68", "BS6"],
    },
    "KR": {
        "exchange": "KSE", "currency": "KRW",
        "symbols": ["005930", "000660", "035420", "051910", "006400", "035720", "068270", "028260", "055550", "105560", "003550", "034730"],
    },
    "AU": {
        "exchange": "ASX", "currency": "AUD",
        "symbols": [
            "CSL", "BHP", "CBA", "WDS", "XRO", "ALL", "WBC", "ANZ",
            "NAB", "FMG", "WOW", "COL", "RIO", "TLS", "REA", "GMG",
            "MQG", "TCL", "SHL", "JHX", "WES", "TWE", "CPU",
        ],
    },
    "IN": {
        "exchange": "NSE", "currency": "INR",
        "symbols": [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
            "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
            "LT", "HCLTECH", "AXISBANK", "WIPRO", "ASIANPAINT",
            "MARUTI", "TITAN", "BAJFINANCE", "NESTLEIND", "TECHM",
        ],
    },
    "ID": {
        "exchange": "IDX", "currency": "IDR",
        "symbols": ["BBCA", "BBRI", "BMRI", "TLKM", "ASII", "UNVR", "HMSP", "GGRM", "ICBP", "KLBF"],
    },
    "IL": {
        "exchange": "TASE", "currency": "ILS",
        "symbols": ["TEVA", "LUMI", "ICL", "BEZQ", "NICE", "CHKP"],
    },
    "ZA": {
        "exchange": "JSE", "currency": "ZAR",
        "symbols": ["NPN", "BTI", "AGL", "SOL", "FSR", "SBK", "BID", "NED", "SHP", "CFR", "MTN"],
    },
    "MX": {
        "exchange": "MEXI", "currency": "MXN",
        "symbols": ["AMXL", "WALMEX", "FEMSAUBD", "GMEXICOB", "GFNORTEO", "BIMBOA", "CEMEXCPO", "AC"],
    },
    "BR": {
        "exchange": "BVMF", "currency": "BRL",
        "symbols": ["VALE3", "PETR4", "ITUB4", "BBDC4", "ABEV3", "B3SA3", "WEGE3", "RENT3", "SUZB3", "RAIL3"],
    },
}

BREAKTHROUGH_PROMPT = """Based on technological (genomics/biotech, AI/compute, nuclear fusion, space, 
advanced materials, quantum computing), demographic, climate, and resource megatrends 
(water scarcity, energy transition, critical minerals), which early-stage companies 
with over $1B market valuation do you see having the fastest growing or most explosive 
returns in a 10-15 year perspective?

Return ONLY a JSON array of up to 30 companies. Each entry must have:
- symbol: stock ticker symbol (US listed preferred, or ADR)
- name: company name
- exchange: SMART for US, or specific exchange code
- currency: USD for US/ADR
- sector: primary sector
- megatrend: which megatrend drives this (1-3 words)
- rationale: why explosive potential (1 sentence max)

Keep rationale under 8 words. Return raw JSON only, no markdown, no explanation."""


def _get_breakthrough_candidates() -> list[dict]:
    try:
        from src.core.config import get_settings
        _ant_key = get_settings().raw.get("anthropic", {}).get("api_key", "")
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": _ant_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": BREAKTHROUGH_PROMPT}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        candidates = json.loads(text.strip())
        print(f"  ✅ Claude returned {len(candidates)} breakthrough candidates")
        return candidates
    except Exception as e:
        print(f"  ❌ Breakthrough scan failed: {e}")
        return []


def _fmp_key() -> Optional[str]:
    try:
        from src.core.config import get_settings
        return get_settings().raw.get("fmp", {}).get("api_key")
    except Exception:
        import os
        return os.environ.get("FMP_API_KEY")


def _fmp_get(endpoint: str, symbol: str, params: dict = {}) -> Optional[list]:
    key = _fmp_key()
    if not key:
        return None
    try:
        all_params = {"symbol": symbol, "apikey": key}
        all_params.update(params)
        r = requests.get(
            f"https://financialmodelingprep.com/stable/{endpoint}",
            params=all_params, timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _get_fmp_fundamentals(symbol: str) -> dict:
    """
    Fetch all fundamental metrics needed for scoring.
    Includes gross margin, FCF margin trend, and dividend CAGR for 10-year total return.
    Uses 4-5 API calls — monthly screener only.
    """
    result = {}

    # ── Income statement: revenue, margins ──
    income = _fmp_get("income-statement", symbol, {"limit": 5})
    if income and len(income) >= 2:
        try:
            rev_latest = income[0].get("revenue", 0)
            rev_prior = income[1].get("revenue", 0)
            rev_oldest = income[-1].get("revenue", 0)
            years = len(income) - 1
            result["revenue_yoy_pct"] = (
                (rev_latest - rev_prior) / abs(rev_prior) * 100 if rev_prior else 0
            )
            result["revenue_avg_pct"] = (
                (rev_latest - rev_oldest) / abs(rev_oldest) * 100 / years
                if rev_oldest and years > 0 else 0
            )
            result["net_income_latest"] = income[0].get("netIncome", 0)
            # Gross margin — key indicator of pricing power and scalability
            gross_profit = income[0].get("grossProfit", 0)
            revenue = income[0].get("revenue", 0)
            if revenue and revenue > 0:
                result["gross_margin_pct"] = gross_profit / revenue * 100
            # Gross margin trend: improving or deteriorating?
            if len(income) >= 3:
                gm_old = income[2].get("grossProfit", 0) / max(income[2].get("revenue", 1), 1) * 100
                gm_new = income[0].get("grossProfit", 0) / max(income[0].get("revenue", 1), 1) * 100
                result["gross_margin_trend"] = gm_new - gm_old  # positive = improving
        except Exception:
            pass

    # ── Ratios: PE, PEG, dividend yield, payout ──
    ratios = _fmp_get("ratios", symbol, {"limit": 3})
    if ratios and len(ratios) >= 1:
        try:
            r = ratios[0]
            result["pe_ratio"] = r.get("priceEarningsRatio") or 0
            result["peg_ratio"] = r.get("priceEarningsToGrowthRatio") or 0
            result["payout_ratio"] = (r.get("payoutRatio") or 0) * 100
            result["dividend_yield"] = (r.get("dividendYield") or 0) * 100
            result["roe"] = (r.get("returnOnEquity") or 0) * 100  # return on equity
            if len(ratios) >= 2:
                prev_yield = (ratios[1].get("dividendYield") or 0) * 100
                curr_yield = result["dividend_yield"]
                result["dividend_cut"] = (prev_yield > 0.5 and curr_yield < prev_yield * 0.7)
            else:
                result["dividend_cut"] = False
        except Exception:
            pass

    # ── Balance sheet: debt ──
    bs = _fmp_get("balance-sheet-statement", symbol, {"limit": 2})
    if bs and len(bs) >= 1:
        try:
            total_debt = bs[0].get("totalDebt") or 0
            equity = bs[0].get("totalStockholdersEquity") or 1
            result["debt_to_equity"] = total_debt / abs(equity) if equity else 0
        except Exception:
            pass

    # ── Cash flow: FCF quality and trend ──
    cf = _fmp_get("cash-flow-statement", symbol, {"limit": 4})
    if cf:
        try:
            result["fcf_negative_years"] = sum(
                1 for c in cf if (c.get("freeCashFlow") or 0) < 0
            )
            result["fcf_latest"] = cf[0].get("freeCashFlow") or 0
            # FCF margin trend: is FCF growing as % of revenue?
            # Positive trend = company converts more revenue to cash over time
            if len(cf) >= 3 and income and len(income) >= 3:
                fcf_old = cf[2].get("freeCashFlow", 0)
                rev_old = income[2].get("revenue", 1) or 1
                fcf_new = cf[0].get("freeCashFlow", 0)
                rev_new = income[0].get("revenue", 1) or 1
                fcf_margin_old = fcf_old / rev_old * 100
                fcf_margin_new = fcf_new / rev_new * 100
                result["fcf_margin_trend"] = fcf_margin_new - fcf_margin_old
        except Exception:
            pass

    # ── Dividend CAGR: the most important factor for 10-year dividend total return ──
    # A 3% yield growing 10%/year beats a 6% yield flat — every time, by year 8
    div_yield = result.get("dividend_yield", 0)
    if div_yield > 0.5:  # only fetch for actual dividend payers
        try:
            hist = _fmp_get("historical-dividends", symbol)
            if hist and isinstance(hist, dict):
                payments = hist.get("historical", [])
            elif hist and isinstance(hist, list):
                payments = hist
            else:
                payments = []
            # Get annual dividend sums for last 5 years
            from collections import defaultdict
            by_year = defaultdict(float)
            for p in payments:
                date_str = p.get("date", "") or p.get("paymentDate", "")
                dividend = p.get("dividend", 0) or p.get("adjDividend", 0)
                if date_str and dividend:
                    year = int(date_str[:4])
                    by_year[year] += float(dividend)
            years_sorted = sorted(by_year.keys(), reverse=True)
            if len(years_sorted) >= 4:
                # 3-year CAGR
                d_new = by_year[years_sorted[0]]
                d_3yr = by_year[years_sorted[3]]
                if d_new > 0 and d_3yr > 0:
                    cagr_3yr = ((d_new / d_3yr) ** (1/3) - 1) * 100
                    result["dividend_cagr_3yr"] = round(cagr_3yr, 1)
            if len(years_sorted) >= 6:
                # 5-year CAGR
                d_5yr = by_year[years_sorted[5]]
                if d_new > 0 and d_5yr > 0:
                    cagr_5yr = ((d_new / d_5yr) ** (1/5) - 1) * 100
                    result["dividend_cagr_5yr"] = round(cagr_5yr, 1)
        except Exception:
            pass

    return result


def _score_growth(fmp: dict) -> float:
    """
    Score growth quality for growth/breakthrough stocks.
    Revenue growth alone is not enough — gross margin shows whether
    growth is profitable and scalable, not just bought with spending.

    Components:
      - Revenue growth (YoY + 3yr avg): 0-60 pts
      - Gross margin level: 0-25 pts (higher margin = better business quality)
      - Gross margin trend: 0-15 pts (improving margins = compounding advantage)
    """
    score = 0.0
    components = 0

    # Revenue growth (60% weight)
    yoy = fmp.get("revenue_yoy_pct", 0)
    avg = fmp.get("revenue_avg_pct", 0)
    rev_scores = []
    if yoy != 0:
        rev_scores.append(min(100, max(0, 30 + yoy * 1.75)))
    if avg != 0:
        rev_scores.append(min(100, max(0, 30 + avg * 1.75)))
    if rev_scores:
        score += (sum(rev_scores) / len(rev_scores)) * 0.60
        components += 1

    # Gross margin level (25% weight)
    # >60% = exceptional (software/pharma), >40% = good, >20% = acceptable, <20% = commodity
    gm = fmp.get("gross_margin_pct")
    if gm is not None:
        if gm >= 60:
            gm_score = 100
        elif gm >= 40:
            gm_score = 70 + (gm - 40) * 1.5
        elif gm >= 20:
            gm_score = 40 + (gm - 20) * 1.5
        else:
            gm_score = max(10, gm * 2)
        score += gm_score * 0.25
        components += 1

    # Gross margin trend (15% weight)
    # Expanding margins = pricing power + operational leverage
    gm_trend = fmp.get("gross_margin_trend")
    if gm_trend is not None:
        if gm_trend > 5:
            trend_score = 100
        elif gm_trend > 2:
            trend_score = 75
        elif gm_trend > 0:
            trend_score = 60
        elif gm_trend > -2:
            trend_score = 40
        else:
            trend_score = 15
        score += trend_score * 0.15
        components += 1

    if components == 0:
        return 50.0
    # Normalize: if not all components available, scale to what we have
    return round(min(100, score), 1)


def _score_valuation(fmp: dict) -> float:
    """
    Score valuation — PEG-first, PE as fallback.
    Critical fix: raw PE penalizes the best compounders.
    A PE of 35 on 30% growth is CHEAPER than a PE of 15 on 5% growth.
    PEG corrects for this. PE alone is only used when PEG unavailable.

    Components:
      - PEG ratio (primary): 0-100 pts — growth-adjusted valuation
      - PE ratio (fallback): 0-100 pts — absolute valuation floor check
      - ROE bonus: 0-10 pts — high ROE at reasonable valuation = exceptional
    """
    score = 0.0
    has_score = False

    peg = fmp.get("peg_ratio", 0)
    pe = fmp.get("pe_ratio", 0)
    roe = fmp.get("roe", 0)

    if peg and peg > 0:
        # PEG < 1 = undervalued relative to growth (ideal)
        # PEG 1-2 = fairly valued
        # PEG > 3 = expensive relative to growth
        if peg < 0.5:
            score = 100
        elif peg < 1.0:
            score = 80 + (1.0 - peg) * 40
        elif peg < 1.5:
            score = 65 - (peg - 1.0) * 30
        elif peg < 2.5:
            score = 40 - (peg - 1.5) * 15
        else:
            score = max(10, 25 - (peg - 2.5) * 6)
        has_score = True
    elif pe and pe > 0:
        # PE-only fallback — gentler penalty than before
        # PE 10 = 90pts, PE 20 = 70pts, PE 30 = 50pts, PE 50 = 20pts
        if pe < 10:
            score = 90
        elif pe < 20:
            score = 90 - (pe - 10) * 2
        elif pe < 35:
            score = 70 - (pe - 20) * 1.3
        else:
            score = max(10, 50 - (pe - 35) * 1.0)
        has_score = True

    if not has_score:
        return 50.0

    # ROE bonus (up to 10 pts) — only meaningful at reasonable PEG
    # High ROE + reasonable PEG = compounding machine
    if roe and roe > 15 and score > 40:
        roe_bonus = min(10, (roe - 15) * 0.4)
        score = min(100, score + roe_bonus)

    return round(score, 1)


def _score_quality(fmp: dict) -> float:
    """
    Score balance sheet and cash flow quality.
    Key insight: FCF margin trend predicts whether price will hold up
    even while dividends are paid. Declining FCF = future dividend cut
    AND price decline — the worst outcome for total return.

    Components:
      - Debt/equity: 0-40 pts (financial stability)
      - FCF consistency: 0-35 pts (cash generation reliability)
      - FCF margin trend: 0-25 pts (structural price support signal)
    """
    scores = []
    weights = []

    # Debt/equity (40% weight)
    de = fmp.get("debt_to_equity")
    if de is not None:
        if de < 0.3:
            de_score = 100
        elif de < 0.7:
            de_score = 85 - (de - 0.3) * 37.5
        elif de < 1.5:
            de_score = 70 - (de - 0.7) * 31.25
        elif de < 3.0:
            de_score = 45 - (de - 1.5) * 20
        else:
            de_score = max(10, 15 - (de - 3.0) * 2)
        scores.append(de_score)
        weights.append(0.40)

    # FCF consistency (35% weight)
    fcf_neg = fmp.get("fcf_negative_years")
    if fcf_neg is not None:
        fcf_score = max(10, 100 - fcf_neg * 30)
        scores.append(fcf_score)
        weights.append(0.35)

    # FCF margin trend (25% weight)
    # This is the forward-looking price protection signal:
    # improving FCF margin = business getting more efficient = price support
    # deteriorating FCF margin = structural problem = future price decline
    fcf_trend = fmp.get("fcf_margin_trend")
    if fcf_trend is not None:
        if fcf_trend > 5:
            trend_score = 100
        elif fcf_trend > 2:
            trend_score = 80
        elif fcf_trend > 0:
            trend_score = 65
        elif fcf_trend > -2:
            trend_score = 45
        elif fcf_trend > -5:
            trend_score = 25
        else:
            trend_score = 10
        scores.append(trend_score)
        weights.append(0.25)

    if not scores:
        return 50.0

    total_weight = sum(weights)
    weighted = sum(s * w for s, w in zip(scores, weights))
    return round(weighted / total_weight, 1)


def _score_dividend_total_return(fmp: dict, growth_score: float) -> float:
    """
    Score for maximum 10-year TOTAL return on dividend stocks.
    Total return = dividends received + price appreciation.

    Critical insight: a stock paying 5% yield while price falls 3%/year
    delivers only 2% real return. Price appreciation is driven by
    earnings/FCF growth. Both must be scored.

    Components (100 pts total):
      - Dividend CAGR (35 pts): the most important long-term factor
        A 3% yield growing 10%/yr beats a 6% flat yield by year 8.
      - Current yield (25 pts): immediate income, but not the whole story
      - Payout sustainability (20 pts): can they keep growing it?
      - Earnings growth / price appreciation proxy (20 pts):
        Revenue + FCF margin trend → will the stock price hold up?

    Dividend cut = immediate disqualifier (score floored at 5).
    """
    div_yield = fmp.get("dividend_yield", 0)
    payout = fmp.get("payout_ratio", 0)
    dividend_cut = fmp.get("dividend_cut", False)
    rev_yoy = fmp.get("revenue_yoy_pct", 0)
    rev_avg = fmp.get("revenue_avg_pct", 0)
    cagr_3yr = fmp.get("dividend_cagr_3yr")
    cagr_5yr = fmp.get("dividend_cagr_5yr")
    fcf_trend = fmp.get("fcf_margin_trend", 0)

    # Dividend cut = structural problem, floor entire score
    if dividend_cut:
        return 5.0

    # ── Dividend CAGR (35 pts) ──
    # Use 5yr CAGR if available, else 3yr, else estimate from revenue growth
    cagr = cagr_5yr if cagr_5yr is not None else cagr_3yr
    if cagr is not None:
        if cagr < 0:
            cagr_score = 0   # dividend shrinking = structural problem
        elif cagr < 2:
            cagr_score = 8   # barely keeping up with inflation
        elif cagr < 5:
            cagr_score = 18  # modest growth
        elif cagr < 8:
            cagr_score = 25  # solid
        elif cagr < 12:
            cagr_score = 30  # strong
        elif cagr < 18:
            cagr_score = 33  # exceptional
        else:
            cagr_score = 35  # elite compounder
    else:
        # No dividend history — estimate from revenue growth as proxy
        rev_growth = max(rev_yoy, rev_avg) if rev_avg else rev_yoy
        cagr_score = min(20, max(0, rev_growth * 1.2))

    # ── Current yield (25 pts) ──
    # Sweet spot: 2.5-6% yield. Below 2% = mostly a growth stock.
    # Above 8% = yield trap risk (market pricing in trouble).
    if div_yield < 1.0:
        yield_score = 2
    elif div_yield < 2.0:
        yield_score = 8
    elif div_yield < 3.0:
        yield_score = 15
    elif div_yield < 4.5:
        yield_score = 22
    elif div_yield < 6.0:
        yield_score = 25
    elif div_yield < 8.0:
        yield_score = 20  # getting into yield-trap territory
    else:
        yield_score = 10  # likely a yield trap — market sees risk

    # ── Payout ratio sustainability (20 pts) ──
    if payout <= 0:
        sustainability = 10  # no data, neutral
    elif payout < 35:
        sustainability = 20  # lots of room to grow dividend
    elif payout < 55:
        sustainability = 17  # healthy
    elif payout < 70:
        sustainability = 12  # manageable but watch FCF
    elif payout < 85:
        sustainability = 6   # stretched — needs earnings growth
    else:
        sustainability = 0   # unsustainable without growth

    # ── Price appreciation proxy (20 pts) ──
    # Earnings/FCF growth drives price over 10 years.
    # A flat-price high-yield stock is a bad 10-year investment.
    rev_growth = (rev_yoy + rev_avg) / 2 if rev_avg else rev_yoy
    earnings_score = min(12, max(0, rev_growth * 0.8))
    # FCF margin trend: improving FCF = price will hold up
    if fcf_trend > 3:
        fcf_price_score = 8
    elif fcf_trend > 0:
        fcf_price_score = 5
    elif fcf_trend > -3:
        fcf_price_score = 2
    else:
        fcf_price_score = 0  # FCF deteriorating = price risk
    price_appreciation_score = min(20, earnings_score + fcf_price_score)

    total = cagr_score + yield_score + sustainability + price_appreciation_score
    return round(min(100, total), 1)

@dataclass
class StockScore:
    symbol: str
    name: str = ""
    exchange: str = "SMART"
    currency: str = "USD"
    sector: str = "Unknown"
    market_cap: float = 0
    price: float = 0
    growth_score: float = 0
    valuation_score: float = 0
    quality_score: float = 0
    dividend_yield: float = 0
    dividend_total_return_score: float = 0
    options_available: bool = False
    options_liquidity: float = 0
    chain_depth: int = 0
    avg_spread_pct: float = 0
    portfolio_score: float = 0
    options_score: float = 0
    tier: str = "growth"
    megatrend: str = ""
    rationale: str = ""
    fundamentals_complete: bool = True  # False when growth/val/quality all defaulted to 50 (no FMP+IBKR data)
    notes: list[str] = field(default_factory=list)


def _check_breakthrough_eligibility(symbol: str, score_market_cap: float = 0) -> tuple[bool, str]:
    """
    For breakthrough-tier candidates ONLY: validate against quality floors.
    Returns (eligible, reason_if_not).

    Filters:
    - Reject ETFs (isEtf=true in FMP profile)
    - Reject if market_cap < 500M (use FMP profile mktCap as backup if score has 0)
    - Reject if any reverse stock split in the last 18 months
    """
    from datetime import datetime, timedelta

    # ── Profile: ETF check + market cap backup ──
    profile = _fmp_get("profile", symbol)
    if profile and len(profile) >= 1:
        try:
            p = profile[0]
            if p.get("isEtf") is True:
                return False, "ETF (isEtf=true)"
            mcap = p.get("mktCap") or 0
            if mcap and (not score_market_cap or score_market_cap == 0):
                score_market_cap = float(mcap)
        except Exception:
            pass

    if score_market_cap and score_market_cap < 500_000_000:
        return False, f"market_cap=${score_market_cap/1e6:.0f}M below $500M floor"

    # ── Stock splits: reverse split detection ──
    splits = _fmp_get("historical-stock-splits", symbol)
    if splits and isinstance(splits, list):
        cutoff = datetime.utcnow() - timedelta(days=18 * 30)
        for s in splits:
            try:
                date_str = s.get("date") or ""
                if not date_str:
                    continue
                split_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
                if split_date < cutoff:
                    continue
                num = float(s.get("numerator") or 0)
                den = float(s.get("denominator") or 0)
                if num > 0 and den > 0 and num < den:
                    label = s.get("label") or f"{int(num)}-for-{int(den)}"
                    return False, f"reverse split {label} on {date_str[:10]}"
            except Exception:
                continue

    return True, ""


class UniverseScreener:
    def __init__(self, ib: IB):
        self.ib = ib

    def screen_all(
        self,
        regions: list[str] | None = None,
        min_market_cap: float = 1e9,
        growth_count: int = 60,
        dividend_count: int = 15,
        breakthrough_count: int = 25,
        options_count: int = 50,
    ) -> tuple[list[StockScore], list[StockScore]]:
        if regions is None:
            regions = list(CANDIDATE_POOLS.keys())

        all_scores: list[StockScore] = []

        print(f"\n{'='*60}")
        print(f"PHASE 1: Screening regular universe ({len(regions)} regions)")
        print(f"{'='*60}")

        for region in regions:
            pool = CANDIDATE_POOLS.get(region)
            if not pool:
                continue
            print(f"\n── {region} — {len(pool['symbols'])} candidates ──")
            for symbol in pool["symbols"]:
                try:
                    score = self._score_stock(
                        symbol=str(symbol),
                        exchange=pool["exchange"],
                        currency=pool["currency"],
                    )
                    if score and score.market_cap >= min_market_cap:
                        all_scores.append(score)
                        status = "✅" if score.options_available else "⛔"
                        print(f"  {status} {score.symbol:8s} | Port: {score.portfolio_score:5.1f} | Opts: {score.options_score:5.1f} | MCap: ${score.market_cap/1e9:6.1f}B | Div: {score.dividend_yield:.1f}%")
                    else:
                        print(f"  ⛔ {symbol:8s} | Below threshold or no data")
                except Exception as e:
                    print(f"  ❌ {symbol:8s} | Error: {e}")
                time.sleep(0.3)

        print(f"\n{'='*60}")
        print(f"\n{'='*60}")
        print(f"PHASE 1b: Screening dedicated dividend candidates")
        print(f"{'='*60}")

        for region, pool in DIVIDEND_CANDIDATES.items():
            print(f"\n\u2500\u2500 {region} \u2014 {len(pool['symbols'])} candidates \u2500\u2500")
            for symbol in pool["symbols"]:
                if any(s.symbol == str(symbol) for s in all_scores):
                    continue
                try:
                    score = self._score_stock(
                        symbol=str(symbol),
                        exchange=pool["exchange"],
                        currency=pool["currency"],
                    )
                    if score and score.market_cap >= min_market_cap:
                        all_scores.append(score)
                        status = "\u2705 " if score.options_available else "\u26d4 "
                        print(f"  {status} {score.symbol:8s} | Port: {score.portfolio_score:5.1f} | Div: {score.dividend_yield:.1f}% | MCap: ${score.market_cap/1e9:6.1f}B")
                    else:
                        print(f"  \u26d4  {symbol:8s} | Below threshold or no data")
                except Exception as e:
                    print(f"  \u274c  {symbol:8s} | Error: {e}")
                time.sleep(0.3)

        print(f"PHASE 2: Breakthrough scan via AI")
        print(f"{'='*60}")

        breakthrough_candidates = _get_breakthrough_candidates()
        breakthrough_scores: list[StockScore] = []

        for candidate in breakthrough_candidates:
            symbol = candidate.get("symbol", "")
            if not symbol:
                continue
            try:
                score = self._score_stock(
                    symbol=symbol,
                    exchange=candidate.get("exchange", "SMART"),
                    currency=candidate.get("currency", "USD"),
                )
                if score:
                    eligible, reject_reason = _check_breakthrough_eligibility(symbol, score.market_cap)
                    if not eligible:
                        print(f"  ⛔  {symbol:8s} | REJECTED: {reject_reason}")
                        continue
                    score.tier = "breakthrough"
                    score.megatrend = candidate.get("megatrend", "")
                    score.rationale = candidate.get("rationale", "")
                    if score.name == symbol:
                        score.name = candidate.get("name", symbol)
                    if score.sector == "Unknown":
                        score.sector = candidate.get("sector", "Unknown")
                    breakthrough_scores.append(score)
                    status = "✅" if score.options_available else "⛔"
                    print(f"  {status} {score.symbol:8s} | MCap: ${score.market_cap/1e9:5.1f}B | {score.megatrend}")
                else:
                    print(f"  ⛔ {symbol:8s} | No IBKR data")
            except Exception as e:
                print(f"  ❌ {symbol:8s} | Error: {e}")
            time.sleep(0.3)

        print(f"\n{'='*60}")
        print(f"PHASE 3: Building portfolio universe")
        print(f"{'='*60}")

        _dividend_pool_symbols = {str(sym) for pool in DIVIDEND_CANDIDATES.values() for sym in pool["symbols"]}
        dividend_candidates = [s for s in all_scores if s.tier != "breakthrough" and (s.symbol in _dividend_pool_symbols or s.dividend_yield > 2.5)]
        growth_candidates = [s for s in all_scores if s.tier != "breakthrough" and s.symbol not in _dividend_pool_symbols and s.dividend_yield <= 2.5]

        dividend_candidates.sort(key=lambda s: s.dividend_total_return_score, reverse=True)
        growth_candidates.sort(key=lambda s: s.portfolio_score, reverse=True)
        breakthrough_scores.sort(key=lambda s: s.market_cap, reverse=True)

        for s in growth_candidates:
            s.tier = "growth"
        for s in dividend_candidates:
            s.tier = "dividend"

        # Dedup: a stock can be in CANDIDATE_POOLS (scored as growth/dividend) AND
        # returned by Claude's breakthrough scan. Without dedup, the same symbol
        # ends up in two tiers and Phase 2 reclassifies it back-and-forth in the
        # same run. Breakthrough wins — Claude's curated thesis assignment is
        # more specific than the pool-membership default.
        breakthrough_symbols = {s.symbol for s in breakthrough_scores}
        growth_candidates = [s for s in growth_candidates if s.symbol not in breakthrough_symbols]
        dividend_candidates = [s for s in dividend_candidates if s.symbol not in breakthrough_symbols]

        selected_growth = growth_candidates[:growth_count]
        selected_dividend = dividend_candidates[:dividend_count]
        selected_breakthrough = breakthrough_scores[:breakthrough_count]

        portfolio_universe = selected_breakthrough + selected_growth + selected_dividend

        print(f"\n  Breakthrough: {len(selected_breakthrough)}")
        print(f"  Growth:       {len(selected_growth)}")
        print(f"  Dividend:     {len(selected_dividend)}")
        print(f"  Total:        {len(portfolio_universe)}")

        print(f"\n{'='*60}")
        print(f"PHASE 4: Building options universe (top {options_count})")
        print(f"{'='*60}")

        options_eligible = [s for s in portfolio_universe if s.options_available]
        options_eligible.sort(key=lambda s: s.options_score, reverse=True)
        options_universe = options_eligible[:options_count]

        print(f"  Options-eligible: {len(options_eligible)}")
        print(f"  Options universe: {len(options_universe)}")

        return portfolio_universe, options_universe, all_scores

    def _score_stock(self, symbol: str, exchange: str, currency: str) -> Optional[StockScore]:
        contract = Stock(symbol, exchange, currency)
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            return None

        score = StockScore(symbol=symbol, exchange=exchange, currency=currency)

        details_list = self.ib.reqContractDetails(contract)
        if details_list:
            details = details_list[0]
            score.name = details.longName or symbol
            score.sector = details.industry or details.category or "Unknown"

        ticker = self.ib.reqMktData(contract, "100,258", False, False)
        self.ib.sleep(3)
        self.ib.cancelMktData(contract)

        price = ticker.last if ticker.last and ticker.last > 0 else ticker.close
        if not price or price <= 0:
            # Fallback to FMP price when IBKR returns no data
            try:
                key = _fmp_key()
                if key:
                    import requests as _req
                    r = _req.get(
                        f"https://financialmodelingprep.com/stable/quote?symbol={symbol}&apikey={key}",
                        timeout=10,
                    )
                    data = r.json()
                    if data and isinstance(data, list) and data[0].get("price"):
                        price = float(data[0]["price"])
                        print(f"  ℹ️  {symbol}: using FMP price ${price:.2f} (IBKR returned no data)")
            except Exception:
                pass
        if not price or price <= 0:
            return None
        # IBKR reports GBP stock prices in pence — convert to pounds
        # (same convention as src/broker/trade_sync.py:318-321)
        if currency == "GBP":
            price = price / 100.0
        score.price = float(price)
        score.market_cap = self._estimate_market_cap(contract, price)

        fmp = _get_fmp_fundamentals(symbol)

        # IBKR fundamentals fallback — covers LSE/AEB/HKEX/etc. where FMP
        # returns nothing, and overrides FMP fields known to be broken
        # (payout_ratio always 0, dividend_cagr_* always None).
        try:
            from src.portfolio.ibkr_fundamentals import get_ibkr_fundamentals
            ibkr = get_ibkr_fundamentals(self.ib, contract, current_price=price)
        except Exception:
            ibkr = {}
        if ibkr:
            # Prefer IBKR for fields where FMP is known unreliable
            for k in ("payout_ratio", "dividend_cagr_3yr", "dividend_cagr_5yr"):
                if k in ibkr and ibkr[k] is not None:
                    fmp[k] = ibkr[k]
            # dividend_cut: prefer IBKR (its detection is actual DPS-based, not yield-based)
            if "dividend_cut" in ibkr:
                fmp["dividend_cut"] = ibkr["dividend_cut"]
            # dividend_yield: fill in when FMP returned 0 or missing
            if ibkr.get("dividend_yield") and not fmp.get("dividend_yield"):
                fmp["dividend_yield"] = ibkr["dividend_yield"]
            # Revenue: fill in when FMP missing
            for k in ("revenue_yoy_pct", "revenue_avg_pct"):
                if k in ibkr and not fmp.get(k):
                    fmp[k] = ibkr[k]
        score.growth_score = _score_growth(fmp)
        score.valuation_score = _score_valuation(fmp)
        score.quality_score = _score_quality(fmp)

        # Detect complete-fundamentals-missing: when all three scorers returned
        # the exact default 50.0, neither FMP nor IBKR had fundamental data for
        # this stock. Flag so the dashboard can surface the uncertainty (stocks
        # can still score well on technical signal, but without quality verification).
        if (score.growth_score == 50.0 and score.valuation_score == 50.0
                and score.quality_score == 50.0):
            score.fundamentals_complete = False

        score.dividend_yield = fmp.get("dividend_yield", 0)
        score.dividend_total_return_score = _score_dividend_total_return(fmp, score.growth_score)

        self._score_options(score, contract)

        score.portfolio_score = round(
            score.growth_score * 0.40 + score.valuation_score * 0.25 + score.quality_score * 0.35, 1
        )
        score.options_score = round(
            score.portfolio_score * 0.60 + score.options_liquidity * 0.40, 1
        )
        if not score.options_available:
            score.options_score *= 0.3

        return score

    def _estimate_market_cap(self, contract: Stock, price: float) -> float:
        try:
            fundamentals = self.ib.reqFundamentalData(contract, "ReportSnapshot")
            if fundamentals:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(fundamentals)
                mcap_elem = root.find(".//Ratios/Group[@ID='Price and Volume']/Ratio[@FieldName='MKTCAP']")
                if mcap_elem is not None and mcap_elem.text:
                    return float(mcap_elem.text) * 1e6
        except Exception:
            pass
        return price * 1e8

    def _score_options(self, score: StockScore, contract: Stock) -> None:
        try:
            chains = self.ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)
            if not chains:
                score.options_available = False
                score.options_liquidity = 0
                return
            chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
            score.options_available = True
            score.chain_depth = len(chain.expirations)
            depth_score = min(100, 20 + score.chain_depth * 3.5)
            spread_score = 70
            near_expiries = sorted(chain.expirations)[:3]
            if near_expiries and chain.strikes:
                atm = min(chain.strikes, key=lambda s: abs(s - score.price))
                test_opt = Option(score.symbol, near_expiries[0], atm, "P", chain.exchange, currency=score.currency)
                qualified = self.ib.qualifyContracts(test_opt)
                if qualified:
                    ticker = self.ib.reqMktData(test_opt, "", False, False)
                    self.ib.sleep(1.5)
                    self.ib.cancelMktData(test_opt)
                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0
                    mid = (bid + ask) / 2 if bid and ask else 0
                    if mid > 0:
                        spread_pct = (ask - bid) / mid * 100
                        score.avg_spread_pct = spread_pct
                        spread_score = max(10, min(100, 100 - spread_pct * 3))
            score.options_liquidity = round(depth_score * 0.4 + spread_score * 0.6, 1)
        except Exception as e:
            score.options_available = False
            score.options_liquidity = 0
            score.notes.append(f"Options check failed: {e}")


def write_screened_universe(stocks: list[StockScore], path: Path) -> None:
    breakthrough = [s for s in stocks if s.tier == "breakthrough"]
    growth = [s for s in stocks if s.tier == "growth"]
    dividend = [s for s in stocks if s.tier == "dividend"]

    def _entry(s: StockScore) -> dict:
        e = {"symbol": s.symbol, "name": s.name, "sector": s.sector,
             "exchange": s.exchange, "currency": s.currency,
             "portfolio_score": s.portfolio_score}
        if s.currency == "JPY":
            e["contract_size"] = 100
        if s.tier == "dividend":
            e["dividend_yield"] = round(s.dividend_yield, 2)
            e["dividend_total_return_score"] = s.dividend_total_return_score
        if s.tier == "breakthrough":
            e["megatrend"] = s.megatrend
            e["rationale"] = s.rationale
        return e

    data = {
        "breakthrough": [_entry(s) for s in breakthrough],
        "growth": [_entry(s) for s in growth],
        "dividend": [_entry(s) for s in dividend],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Screened universe — generated {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"# {len(breakthrough)} breakthrough + {len(growth)} growth + {len(dividend)} dividend = {len(stocks)} total\n"
        f"# DO NOT edit manually — regenerated monthly by screener job\n\n"
    )
    with open(path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"\n✅ Screened universe written to {path}")


def write_options_universe(stocks: list[StockScore], path: Path) -> None:
    entries = []
    for rank, s in enumerate(stocks, 1):
        e = {"symbol": s.symbol, "name": s.name, "sector": s.sector,
             "exchange": s.exchange, "currency": s.currency, "tier": s.tier,
             "options_score": s.options_score, "options_liquidity": s.options_liquidity,
             "rank": rank}
        if s.currency == "JPY":
            e["contract_size"] = 100
        entries.append(e)
    data = {"stocks": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Options trading universe — generated {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"# Top {len(stocks)} stocks ranked by options_score (fundamentals + liquidity)\n"
        f"# DO NOT edit manually — regenerated monthly by screener job\n\n"
    )
    with open(path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"✅ Options universe written to {path}")


def print_results(portfolio_universe: list[StockScore], options_universe: list[StockScore]) -> None:
    print(f"\n{'='*100}")
    print(f"  PORTFOLIO UNIVERSE — {len(portfolio_universe)} Stocks")
    print(f"{'='*100}")
    print(f"  {'#':>3}  {'Symbol':8s}  {'Name':25s}  {'Tier':12s}  {'Port':>6}  {'Growth':>6}  {'Value':>6}  {'Quality':>7}  {'Div%':>5}  {'Megatrend':15s}")
    print(f"  {'─'*96}")
    for i, s in enumerate(portfolio_universe, 1):
        megatrend = s.megatrend if s.tier == "breakthrough" else ""
        print(f"  {i:3d}  {s.symbol:8s}  {s.name[:25]:25s}  {s.tier:12s}  {s.portfolio_score:6.1f}  {s.growth_score:6.1f}  {s.valuation_score:6.1f}  {s.quality_score:7.1f}  {s.dividend_yield:5.1f}  {megatrend:15s}")
    print(f"\n{'='*80}")
    print(f"  OPTIONS UNIVERSE — Top {len(options_universe)}")
    print(f"{'='*80}")
    print(f"  {'#':>3}  {'Symbol':8s}  {'Name':25s}  {'Tier':12s}  {'OptScore':>8}  {'OptLiq':>6}  {'Spread%':>7}")
    print(f"  {'─'*76}")
    for i, s in enumerate(options_universe, 1):
        print(f"  {i:3d}  {s.symbol:8s}  {s.name[:25]:25s}  {s.tier:12s}  {s.options_score:8.1f}  {s.options_liquidity:6.1f}  {s.avg_spread_pct:7.1f}")


def main():
    parser = argparse.ArgumentParser(description="Monthly universe screener")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--region", default="all")
    parser.add_argument("--min-mcap", type=float, default=1e9)
    parser.add_argument("--growth", type=int, default=60)
    parser.add_argument("--dividend", type=int, default=15)
    parser.add_argument("--breakthrough", type=int, default=25)
    parser.add_argument("--options-count", type=int, default=50)
    parser.add_argument("--universe-output", default="config/screened_universe.yaml")
    parser.add_argument("--options-output", default="config/options_universe.yaml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    regions = None if args.region == "all" else [r.strip() for r in args.region.split(",")]

    ib = IB()
    try:
        ib.connect("127.0.0.1", args.port, clientId=99)
        print(f"✅ Connected to IBKR on port {args.port}")
        screener = UniverseScreener(ib)
        portfolio_universe, options_universe, all_scores = screener.screen_all(
            regions=regions,
            min_market_cap=args.min_mcap,
            growth_count=args.growth,
            dividend_count=args.dividend,
            breakthrough_count=args.breakthrough,
            options_count=args.options_count,
        )
        print_results(portfolio_universe, options_universe)
        if not args.dry_run:
            write_screened_universe(portfolio_universe, Path(args.universe_output))
            write_options_universe(options_universe, Path(args.options_output))
    finally:
        ib.disconnect()
        print("\n✅ Disconnected from IBKR")


if __name__ == "__main__":
    main()
