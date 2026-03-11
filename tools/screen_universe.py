"""
Watchlist Screener — Monthly Universe Refresh Tool

Screens global stocks across three tiers:
  - Breakthrough (25): Early-stage high-potential companies via AI scan
  - Growth (50): High revenue growth, strong fundamentals globally
  - Dividend (25): Best 10-year total return forecast (price + yield)

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

Return raw JSON only, no markdown, no explanation."""


def _get_breakthrough_candidates() -> list[dict]:
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
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
    result = {}
    income = _fmp_get("income-statement", symbol, {"limit": 4})
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
        except Exception:
            pass

    ratios = _fmp_get("ratios", symbol, {"limit": 2})
    if ratios and len(ratios) >= 1:
        try:
            r = ratios[0]
            result["pe_ratio"] = r.get("priceEarningsRatio") or 0
            result["peg_ratio"] = r.get("priceEarningsToGrowthRatio") or 0
            result["payout_ratio"] = (r.get("payoutRatio") or 0) * 100
            result["dividend_yield"] = (r.get("dividendYield") or 0) * 100
            if len(ratios) >= 2:
                prev_yield = (ratios[1].get("dividendYield") or 0) * 100
                curr_yield = result["dividend_yield"]
                result["dividend_cut"] = (prev_yield > 0.5 and curr_yield < prev_yield * 0.7)
            else:
                result["dividend_cut"] = False
        except Exception:
            pass

    bs = _fmp_get("balance-sheet-statement", symbol, {"limit": 2})
    if bs and len(bs) >= 1:
        try:
            total_debt = bs[0].get("totalDebt") or 0
            equity = bs[0].get("totalStockholdersEquity") or 1
            result["debt_to_equity"] = total_debt / abs(equity) if equity else 0
        except Exception:
            pass

    cf = _fmp_get("cash-flow-statement", symbol, {"limit": 3})
    if cf:
        try:
            result["fcf_negative_years"] = sum(
                1 for c in cf if (c.get("freeCashFlow") or 0) < 0
            )
            result["fcf_latest"] = cf[0].get("freeCashFlow") or 0
        except Exception:
            pass

    return result


def _score_growth(fmp: dict) -> float:
    scores = []
    yoy = fmp.get("revenue_yoy_pct", 0)
    avg = fmp.get("revenue_avg_pct", 0)
    if yoy != 0:
        scores.append(min(100, max(0, 30 + yoy * 1.75)))
    if avg != 0:
        scores.append(min(100, max(0, 30 + avg * 1.75)))
    return round(sum(scores) / len(scores), 1) if scores else 50.0


def _score_valuation(fmp: dict) -> float:
    scores = []
    pe = fmp.get("pe_ratio", 0)
    peg = fmp.get("peg_ratio", 0)
    if pe and pe > 0:
        scores.append(max(10, min(100, 110 - pe * 2.5)))
    if peg and peg > 0:
        scores.append(max(10, min(100, 100 - peg * 30)))
    return round(sum(scores) / len(scores), 1) if scores else 50.0


def _score_quality(fmp: dict) -> float:
    scores = []
    de = fmp.get("debt_to_equity", 0)
    fcf_neg = fmp.get("fcf_negative_years", 0)
    if de is not None:
        scores.append(max(10, min(100, 95 - de * 30)))
    if fcf_neg is not None:
        scores.append(max(10, 90 - fcf_neg * 30))
    return round(sum(scores) / len(scores), 1) if scores else 50.0


def _score_dividend_total_return(fmp: dict, growth_score: float) -> float:
    div_yield = fmp.get("dividend_yield", 0)
    payout = fmp.get("payout_ratio", 0)
    dividend_cut = fmp.get("dividend_cut", False)
    rev_yoy = fmp.get("revenue_yoy_pct", 0)
    yield_score = min(40, div_yield * 8)
    if dividend_cut:
        sustainability = 0
    elif payout <= 0:
        sustainability = 15
    elif payout < 40:
        sustainability = 30
    elif payout < 60:
        sustainability = 20
    elif payout < 80:
        sustainability = 10
    else:
        sustainability = 0
    growth_component = min(30, max(0, rev_yoy * 1.5))
    return round(min(100, yield_score + sustainability + growth_component), 1)

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
    notes: list[str] = field(default_factory=list)


class UniverseScreener:
    def __init__(self, ib: IB):
        self.ib = ib

    def screen_all(
        self,
        regions: list[str] | None = None,
        min_market_cap: float = 1e9,
        growth_count: int = 50,
        dividend_count: int = 25,
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

        dividend_candidates = [s for s in all_scores if s.dividend_yield > 2.5 and s.tier != "breakthrough"]
        growth_candidates = [s for s in all_scores if s.dividend_yield <= 2.5 and s.tier != "breakthrough"]

        dividend_candidates.sort(key=lambda s: s.dividend_total_return_score, reverse=True)
        growth_candidates.sort(key=lambda s: s.portfolio_score, reverse=True)
        breakthrough_scores.sort(key=lambda s: s.market_cap, reverse=True)

        for s in growth_candidates:
            s.tier = "growth"
        for s in dividend_candidates:
            s.tier = "dividend"

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

        return portfolio_universe, options_universe

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
            return None
        score.price = float(price)
        score.market_cap = self._estimate_market_cap(contract, price)

        fmp = _get_fmp_fundamentals(symbol)
        score.growth_score = _score_growth(fmp)
        score.valuation_score = _score_valuation(fmp)
        score.quality_score = _score_quality(fmp)
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
    parser.add_argument("--growth", type=int, default=50)
    parser.add_argument("--dividend", type=int, default=25)
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
        portfolio_universe, options_universe = screener.screen_all(
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
