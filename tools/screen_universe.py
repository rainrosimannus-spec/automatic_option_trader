"""
Watchlist Screener — Annual Universe Refresh Tool

Connects to IBKR and screens global stocks for wheel strategy suitability.
Ranks candidates by a composite score of:
  - Forward growth estimates (earnings, revenue)
  - Valuation (PEG ratio, forward P/E relative to sector)
  - Options liquidity (bid-ask spread, open interest, chain availability)
  - Dividend yield (for the dividend bucket)
  - Balance sheet quality (debt/equity, free cash flow)

Usage:
    python -m tools.screen_universe
    python -m tools.screen_universe --top 50 --output config/watchlist_new.yaml
    python -m tools.screen_universe --region all --min-mcap 10e9

The tool produces a ranked list and optionally writes a new watchlist.yaml.
You review and approve before it goes live.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# Ensure event loop for ib_insync
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, Index, Option

# ── Candidate pools by region ───────────────────────────────
# These are broad screening pools — the tool will filter down to the best
# These are broad screening pools — the tool filters down to the best.
# Markets included: anywhere IBKR supports options trading.
# The screener verifies options availability per stock and penalizes illiquid ones.
CANDIDATE_POOLS = {
    # ═══════════════════════════════════════════════════════════
    # NORTH AMERICA
    # ═══════════════════════════════════════════════════════════
    "US": {
        "exchange": "SMART",
        "currency": "USD",
        "symbols": [
            # Tech
            "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
            "CRM", "AMD", "NFLX", "ADBE", "NOW", "UBER", "PLTR", "PANW",
            "CRWD", "SHOP", "COIN", "MELI", "ANET", "DDOG", "TTD", "NET",
            "ARM", "SNOW", "ABNB", "SQ", "RIVN", "SOFI", "RBLX", "DASH",
            "ORCL", "INTC", "QCOM", "MU", "LRCX", "KLAC", "CDNS", "SNPS",
            # Healthcare
            "LLY", "UNH", "ABBV", "JNJ", "MRK", "PFE", "TMO", "ABT",
            "ISRG", "VRTX", "REGN", "DXCM", "MRNA", "GILD", "AMGN",
            # Financials
            "JPM", "V", "MA", "GS", "BLK", "SCHW", "AXP", "C", "MS",
            # Consumer
            "COST", "WMT", "HD", "NKE", "SBUX", "MCD", "PG", "KO", "PEP",
            "TGT", "LOW", "BKNG",
            # Industrials
            "CAT", "DE", "GE", "RTX", "LMT", "UNP", "HON", "BA",
            # Energy
            "XOM", "CVX", "SLB", "OXY", "COP", "EOG",
        ],
    },
    "CA": {
        "exchange": "SMART",
        "currency": "CAD",
        "primary_exchange": "TSE",   # Toronto — disambiguate from Tokyo
        "symbols": [
            "SHOP", "RY", "TD", "ENB", "CNR", "CP", "BMO", "BNS",
            "SU", "TRP", "BCE", "MFC", "ATD", "CNQ", "WCN", "CSU",
            "BAM", "FTS", "QSR", "LSPD",
        ],
    },
    # ═══════════════════════════════════════════════════════════
    # EUROPE — WESTERN
    # ═══════════════════════════════════════════════════════════
    "UK": {
        "exchange": "LSE",
        "currency": "GBP",
        "symbols": [
            "SHEL", "AZN", "ULVR", "HSBA", "BP", "GSK", "RIO", "LSEG",
            "REL", "DGE", "BATS", "ABF", "PRU", "LLOY", "BARC",
            "VOD", "NG", "SSE", "AAL", "GLEN", "EXPN",
            "CPG", "IMB", "TSCO", "ANTO", "RKT", "CRH", "SMIN",
        ],
    },
    "DE": {
        "exchange": "IBIS",          # Xetra / Frankfurt
        "currency": "EUR",
        "symbols": [
            "SAP", "SIE", "ALV", "MUV2", "DTE", "BAS", "BAYN", "BMW",
            "MBG", "ADS", "IFX", "DBK", "HEN3", "MRK", "FRE",
            "VOW3", "RHM", "SHL", "DHL", "AIR", "MTX", "QIA",
        ],
    },
    "FR": {
        "exchange": "SBF",           # Euronext Paris
        "currency": "EUR",
        "symbols": [
            "MC", "OR", "TTE", "SAN", "AI", "SU", "BN", "CS",
            "AIR", "SAF", "RI", "KER", "DSY", "CAP", "HO",
            "SGO", "DG", "RMS", "STM", "ACA", "BNP",
        ],
    },
    "NL": {
        "exchange": "AEB",           # Euronext Amsterdam
        "currency": "EUR",
        "symbols": [
            "ASML", "INGA", "PHIA", "AD", "WKL", "UNA", "HEIA",
            "AKZA", "ASM", "BESI", "PRX", "REN", "ABN", "RAND",
        ],
    },
    "CH": {
        "exchange": "SWX",
        "currency": "CHF",
        "symbols": [
            "NESN", "NOVN", "ROG", "SIKA", "LONN", "GIVN", "GEBN",
            "UBSG", "ZURN", "SREN", "ABBN", "SLHN",
            "PGHN", "TEMN", "VACN", "LOGN", "AMS", "BARN", "SCMN",
        ],
    },
    "BE": {
        "exchange": "ENEXT.BE",      # Euronext Brussels
        "currency": "EUR",
        "symbols": [
            "ABI", "UCB", "KBC", "SOLB", "ACKB", "AGS", "COLR",
        ],
    },
    "IE": {
        "exchange": "ISE",           # Euronext Dublin
        "currency": "EUR",
        "symbols": [
            "CRH", "RYA", "KRX", "SKG", "FLT",
        ],
    },
    "ES": {
        "exchange": "BM",            # Bolsa de Madrid
        "currency": "EUR",
        "symbols": [
            "SAN", "BBVA", "ITX", "IBE", "TEF", "FER",
            "REP", "AENA", "GRF", "CLNX", "ENG",
        ],
    },
    "IT": {
        "exchange": "BVME",          # Borsa Italiana
        "currency": "EUR",
        "symbols": [
            "ENEL", "ISP", "UCG", "ENI", "STM", "RACE",
            "CNHI", "TEN", "AMP", "MONC",
        ],
    },
    "AT": {
        "exchange": "VSE",           # Vienna
        "currency": "EUR",
        "symbols": [
            "VOE", "OMV", "EBS", "VER", "RBI",
        ],
    },
    # ═══════════════════════════════════════════════════════════
    # EUROPE — NORDIC
    # ═══════════════════════════════════════════════════════════
    "SE": {
        "exchange": "SFB",           # Nasdaq Stockholm
        "currency": "SEK",
        "symbols": [
            "ATCO-A", "INVE-B", "VOLV-B", "SAND", "ERIC-B", "HEXA-B",
            "ASSA-B", "ALFA", "SEB-A", "SWED-A", "SHB-A", "ESSITY-B",
            "EVO", "SINCH", "HMS",
        ],
    },
    "DK": {
        "exchange": "CSE",           # Nasdaq Copenhagen
        "currency": "DKK",
        "symbols": [
            "NOVO-B", "MAERSK-B", "DSV", "VWS", "CARL-B", "COLO-B",
            "NZYM-B", "ORSTED", "PNDORA", "GN", "DEMANT", "FLS",
        ],
    },
    "FI": {
        "exchange": "HEX",           # Nasdaq Helsinki
        "currency": "EUR",
        "symbols": [
            "NOKIA", "SAMPO", "NESTE", "UPM", "FORTUM", "KNEBV",
            "STERV", "ELISA",
        ],
    },
    "NO": {
        "exchange": "OSE",
        "currency": "NOK",
        "symbols": [
            "EQNR", "MOWI", "DNB", "TEL", "ORK", "SALM", "YAR",
            "AKRBP", "SUBC", "AKER", "SCHA", "BAKKA",
        ],
    },
    # ═══════════════════════════════════════════════════════════
    # ASIA-PACIFIC
    # ═══════════════════════════════════════════════════════════
    "JP": {
        "exchange": "TSEJ",          # Tokyo Stock Exchange
        "currency": "JPY",
        "contract_size": 100,
        "symbols": [
            "6758", "6861", "7203", "6367", "8306", "9984", "6902",
            "7741", "4063", "6501", "7267", "8035", "9983", "4502",
            "6098", "3382", "2802", "6273", "7974", "4661",
            "6594", "6723", "6752", "8001", "8766", "9432", "9433",
        ],
    },
    "HK": {
        "exchange": "SEHK",          # Hong Kong Stock Exchange
        "currency": "HKD",
        "symbols": [
            "0700", "9988", "0005", "0941", "2318", "0388", "1299",
            "0883", "2269", "1211", "0001", "0016", "0002", "0066",
            "1810", "0669", "3690", "9618", "0175", "1928",
        ],
    },
    "SG": {
        "exchange": "SGX",           # Singapore Exchange
        "currency": "SGD",
        "symbols": [
            "D05", "O39", "U11", "Z74", "BN4", "C6L", "C38U",
            "A17U", "G13", "S58", "F34", "S68", "BS6",
        ],
    },
    "KR": {
        "exchange": "KSE",           # Korea Exchange
        "currency": "KRW",
        "symbols": [
            "005930", "000660", "035420", "051910", "006400", "035720",
            "068270", "028260", "055550", "105560", "003550", "034730",
        ],
    },
    "AU": {
        "exchange": "ASX",
        "currency": "AUD",
        "symbols": [
            "CSL", "BHP", "CBA", "WDS", "XRO", "ALL", "WBC", "ANZ",
            "NAB", "FMG", "WOW", "COL", "RIO", "TLS", "REA", "GMG",
            "MQG", "TCL", "SHL", "JHX", "WES", "TWE", "CPU",
        ],
    },
    "IN": {
        "exchange": "NSE",           # National Stock Exchange of India
        "currency": "INR",
        "symbols": [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
            "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
            "LT", "HCLTECH", "AXISBANK", "WIPRO", "ASIANPAINT",
            "MARUTI", "TITAN", "BAJFINANCE", "NESTLEIND", "TECHM",
        ],
    },
    "ID": {
        "exchange": "IDX",           # Indonesia Stock Exchange
        "currency": "IDR",
        "symbols": [
            "BBCA", "BBRI", "BMRI", "TLKM", "ASII", "UNVR",
            "HMSP", "GGRM", "ICBP", "KLBF",
        ],
    },
    # ═══════════════════════════════════════════════════════════
    # MIDDLE EAST & AFRICA
    # ═══════════════════════════════════════════════════════════
    "IL": {
        "exchange": "TASE",          # Tel Aviv Stock Exchange
        "currency": "ILS",
        "symbols": [
            "TEVA", "LUMI", "ICL", "BEZQ", "NICE", "CHKP",
        ],
    },
    "ZA": {
        "exchange": "JSE",           # Johannesburg Stock Exchange
        "currency": "ZAR",
        "symbols": [
            "NPN", "BTI", "AGL", "SOL", "FSR", "SBK",
            "BID", "NED", "SHP", "CFR", "MTN",
        ],
    },
    # ═══════════════════════════════════════════════════════════
    # LATIN AMERICA
    # ═══════════════════════════════════════════════════════════
    "MX": {
        "exchange": "MEXI",          # Mexican Stock Exchange
        "currency": "MXN",
        "symbols": [
            "AMXL", "WALMEX", "FEMSAUBD", "GMEXICOB", "GFNORTEO",
            "BIMBOA", "CEMEXCPO", "AC",
        ],
    },
    "BR": {
        "exchange": "BVMF",          # B3 (Brazil)
        "currency": "BRL",
        "symbols": [
            "VALE3", "PETR4", "ITUB4", "BBDC4", "ABEV3", "B3SA3",
            "WEGE3", "RENT3", "SUZB3", "RAIL3",
        ],
    },
}



@dataclass
class StockScore:
    """Composite score for a candidate stock."""
    symbol: str
    name: str = ""
    exchange: str = "SMART"
    currency: str = "USD"
    sector: str = "Unknown"
    market_cap: float = 0
    price: float = 0
    # Fundamental scores (0-100 each)
    growth_score: float = 0       # forward earnings/revenue growth
    valuation_score: float = 0    # PEG, forward P/E
    quality_score: float = 0      # balance sheet, FCF
    dividend_yield: float = 0
    # Options scores
    options_available: bool = False
    options_liquidity: float = 0  # 0-100 based on spread and OI
    chain_depth: int = 0          # number of expiries available
    avg_spread_pct: float = 0     # average bid-ask spread as % of mid
    # Composite
    composite_score: float = 0
    category: str = "growth"      # "growth" or "dividend"
    notes: list[str] = field(default_factory=list)


class UniverseScreener:
    """Screen and rank stocks for the options wheel universe."""

    def __init__(self, ib: IB):
        self.ib = ib

    def screen_all(
        self,
        regions: list[str] | None = None,
        min_market_cap: float = 5e9,
        top_n: int = 50,
        growth_count: int = 40,
        dividend_count: int = 10,
    ) -> list[StockScore]:
        """
        Screen all candidate pools and return the top N ranked stocks.
        """
        if regions is None:
            regions = list(CANDIDATE_POOLS.keys())

        all_scores: list[StockScore] = []

        for region in regions:
            pool = CANDIDATE_POOLS.get(region)
            if not pool:
                print(f"⚠ Unknown region: {region}")
                continue

            print(f"\n{'='*60}")
            print(f"Screening {region} — {len(pool['symbols'])} candidates")
            print(f"Exchange: {pool['exchange']}, Currency: {pool['currency']}")
            print(f"{'='*60}")

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
                        print(f"  {status} {score.symbol:8s} | Score: {score.composite_score:5.1f} | "
                              f"MCap: ${score.market_cap/1e9:6.1f}B | Options: {score.options_liquidity:.0f}/100")
                    else:
                        print(f"  ⛔ {symbol:8s} | Below market cap threshold or no data")
                except Exception as e:
                    print(f"  ❌ {symbol:8s} | Error: {e}")
                time.sleep(0.5)  # rate limiting

        # Sort by composite score
        all_scores.sort(key=lambda s: s.composite_score, reverse=True)

        # Split into growth and dividend buckets
        # Dividend candidates: div yield > 2.5%
        dividend_candidates = [s for s in all_scores if s.dividend_yield > 2.5]
        growth_candidates = [s for s in all_scores if s.dividend_yield <= 2.5]

        # Take top N from each bucket
        selected_growth = growth_candidates[:growth_count]
        selected_dividend = dividend_candidates[:dividend_count]

        # If not enough dividends, fill from growth
        if len(selected_dividend) < dividend_count:
            remaining = dividend_count - len(selected_dividend)
            extra = [s for s in all_scores if s not in selected_growth and s not in selected_dividend]
            selected_dividend.extend(extra[:remaining])

        for s in selected_growth:
            s.category = "growth"
        for s in selected_dividend:
            s.category = "dividend"

        selected = selected_growth + selected_dividend
        return selected

    def _score_stock(
        self,
        symbol: str,
        exchange: str,
        currency: str,
    ) -> Optional[StockScore]:
        """Score a single stock across all dimensions."""
        contract = Stock(symbol, exchange, currency)
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            return None

        score = StockScore(
            symbol=symbol,
            exchange=exchange,
            currency=currency,
        )

        # Get contract details for fundamentals
        details_list = self.ib.reqContractDetails(contract)
        if details_list:
            details = details_list[0]
            score.name = details.longName or symbol
            score.sector = details.industry or details.category or "Unknown"

        # Get market data for price
        ticker = self.ib.reqMktData(contract, "100,258", False, False)
        self.ib.sleep(3)
        self.ib.cancelMktData(contract)

        price = ticker.last if ticker.last and ticker.last > 0 else ticker.close
        if not price or price <= 0:
            return None
        score.price = float(price)

        # Market cap (approximate from price * shares if available)
        # Use fundamental data if available
        score.market_cap = self._estimate_market_cap(contract, price)

        # Fundamental scores
        score.growth_score = self._score_growth(contract)
        score.valuation_score = self._score_valuation(contract, price)
        score.quality_score = self._score_quality(contract)
        score.dividend_yield = self._get_dividend_yield(ticker)

        # Options scoring
        self._score_options(score, contract)

        # Composite score
        # Weights: growth 30%, valuation 20%, quality 15%, options 25%, dividend 10%
        score.composite_score = (
            score.growth_score * 0.30
            + score.valuation_score * 0.20
            + score.quality_score * 0.15
            + score.options_liquidity * 0.25
            + min(score.dividend_yield * 10, 10) * 0.10  # cap at 10 points
        )

        # Bonus for options availability
        if not score.options_available:
            score.composite_score *= 0.3  # severe penalty
            score.notes.append("No options available")

        return score

    def _estimate_market_cap(self, contract: Stock, price: float) -> float:
        """Estimate market cap. Falls back to fundamentals or price heuristic."""
        try:
            # Try fundamental data
            fundamentals = self.ib.reqFundamentalData(contract, "ReportSnapshot")
            if fundamentals:
                # Parse XML for market cap
                import xml.etree.ElementTree as ET
                root = ET.fromstring(fundamentals)
                mcap_elem = root.find(".//Ratios/Group[@ID='Price and Volume']/Ratio[@FieldName='MKTCAP']")
                if mcap_elem is not None and mcap_elem.text:
                    return float(mcap_elem.text) * 1e6  # usually in millions
        except Exception:
            pass

        # Rough heuristic fallback — not accurate but prevents filtering out unknowns
        return price * 1e8  # assume ~100M shares as rough estimate

    def _score_growth(self, contract: Stock) -> float:
        """Score forward growth (0-100) based on fundamental data."""
        try:
            fundamentals = self.ib.reqFundamentalData(contract, "ReportSnapshot")
            if not fundamentals:
                return 50  # neutral

            import xml.etree.ElementTree as ET
            root = ET.fromstring(fundamentals)

            # Look for earnings growth estimates
            growth_fields = ["EPSGROWTHR", "REVGROWTHR", "TARGETPRICE"]
            scores = []

            for field_name in growth_fields:
                elem = root.find(f".//Ratio[@FieldName='{field_name}']")
                if elem is not None and elem.text:
                    val = float(elem.text)
                    # Normalize: 0% growth = 30, 20% = 70, 40%+ = 100
                    normalized = min(100, max(0, 30 + val * 1.75))
                    scores.append(normalized)

            return sum(scores) / len(scores) if scores else 50

        except Exception:
            return 50  # neutral fallback

    def _score_valuation(self, contract: Stock, price: float) -> float:
        """Score valuation attractiveness (0-100). Lower P/E = higher score."""
        try:
            fundamentals = self.ib.reqFundamentalData(contract, "ReportSnapshot")
            if not fundamentals:
                return 50

            import xml.etree.ElementTree as ET
            root = ET.fromstring(fundamentals)

            # Forward P/E
            pe_elem = root.find(".//Ratio[@FieldName='APENORM']")
            peg_elem = root.find(".//Ratio[@FieldName='PEGRATIOAVG']")

            scores = []

            if pe_elem is not None and pe_elem.text:
                pe = float(pe_elem.text)
                # P/E < 15 = excellent (90), 15-25 = good (60-80), > 40 = poor (20)
                if pe > 0:
                    pe_score = max(10, min(100, 110 - pe * 2.5))
                    scores.append(pe_score)

            if peg_elem is not None and peg_elem.text:
                peg = float(peg_elem.text)
                # PEG < 1 = undervalued (90), 1-2 = fair (60), > 3 = expensive (20)
                if peg > 0:
                    peg_score = max(10, min(100, 100 - peg * 30))
                    scores.append(peg_score)

            return sum(scores) / len(scores) if scores else 50

        except Exception:
            return 50

    def _score_quality(self, contract: Stock) -> float:
        """Score balance sheet quality (0-100)."""
        try:
            fundamentals = self.ib.reqFundamentalData(contract, "ReportSnapshot")
            if not fundamentals:
                return 50

            import xml.etree.ElementTree as ET
            root = ET.fromstring(fundamentals)

            scores = []

            # Debt/Equity
            de_elem = root.find(".//Ratio[@FieldName='TTMDEBT2EQUITY']")
            if de_elem is not None and de_elem.text:
                de = float(de_elem.text)
                # D/E < 0.5 = excellent (90), < 1 = good (70), > 2 = poor (30)
                de_score = max(10, min(100, 95 - de * 30))
                scores.append(de_score)

            # ROE
            roe_elem = root.find(".//Ratio[@FieldName='TTMROEPCT']")
            if roe_elem is not None and roe_elem.text:
                roe = float(roe_elem.text)
                # ROE > 20% = excellent (90), 10-20% = good (70), < 0 = poor (10)
                roe_score = max(10, min(100, 30 + roe * 3))
                scores.append(roe_score)

            return sum(scores) / len(scores) if scores else 50

        except Exception:
            return 50

    def _get_dividend_yield(self, ticker) -> float:
        """Extract dividend yield from ticker data."""
        try:
            if hasattr(ticker, 'dividends') and ticker.dividends:
                # Sum trailing 12 month dividends
                total_div = sum(d.amount for d in ticker.dividends if d.amount)
                if total_div > 0 and ticker.last and ticker.last > 0:
                    return (total_div / ticker.last) * 100
        except Exception:
            pass

        # Try from fundamental ratios
        try:
            if hasattr(ticker, 'fundamentalRatios') and ticker.fundamentalRatios:
                fr = ticker.fundamentalRatios
                if hasattr(fr, 'yield_') and fr.yield_:
                    return float(fr.yield_)
        except Exception:
            pass

        return 0.0

    def _score_options(self, score: StockScore, contract: Stock) -> None:
        """Score options liquidity and availability."""
        try:
            chains = self.ib.reqSecDefOptParams(
                contract.symbol, "", contract.secType, contract.conId
            )

            if not chains:
                score.options_available = False
                score.options_liquidity = 0
                return

            # Find the best chain (prefer SMART)
            chain = None
            for c in chains:
                if c.exchange == "SMART":
                    chain = c
                    break
            if not chain:
                chain = chains[0]

            score.options_available = True
            score.chain_depth = len(chain.expirations)

            # Score based on chain depth
            # 1-5 expiries = poor (30), 6-20 = good (60), 20+ = excellent (90)
            depth_score = min(100, 20 + score.chain_depth * 3.5)

            # Try to get spread data on the nearest ATM option
            spread_score = 70  # default decent
            today = datetime.now().date()
            near_expiries = sorted(chain.expirations)[:3]

            if near_expiries and chain.strikes:
                # Find ATM strike
                atm = min(chain.strikes, key=lambda s: abs(s - score.price))
                test_opt = Option(
                    score.symbol, near_expiries[0], atm, "P",
                    chain.exchange, currency=score.currency
                )
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
                        # Spread < 5% = excellent (90), 5-15% = ok (60), > 30% = poor (20)
                        spread_score = max(10, min(100, 100 - spread_pct * 3))

            score.options_liquidity = (depth_score * 0.4 + spread_score * 0.6)

        except Exception as e:
            score.options_available = False
            score.options_liquidity = 0
            score.notes.append(f"Options check failed: {e}")


def generate_watchlist_yaml(stocks: list[StockScore], path: Path) -> None:
    """Write a watchlist.yaml from scored stocks."""
    growth = [s for s in stocks if s.category == "growth"]
    dividend = [s for s in stocks if s.category == "dividend"]

    data = {
        "# Generated": f"{datetime.now().strftime('%Y-%m-%d')} by screen_universe tool",
        "growth": [],
        "dividend": [],
    }

    for s in growth:
        entry = {
            "symbol": s.symbol,
            "name": s.name,
            "sector": s.sector,
            "exchange": s.exchange,
            "currency": s.currency,
        }
        if s.currency == "JPY":
            entry["contract_size"] = 100
        data["growth"].append(entry)

    for s in dividend:
        entry = {
            "symbol": s.symbol,
            "name": s.name,
            "sector": s.sector,
            "exchange": s.exchange,
            "currency": s.currency,
            "div_yield": round(s.dividend_yield, 1),
        }
        if s.currency == "JPY":
            entry["contract_size"] = 100
        data["dividend"].append(entry)

    # Write without the comment key
    del data["# Generated"]
    header = f"# Watchlist generated {datetime.now().strftime('%Y-%m-%d')} by screen_universe\n"
    header += f"# {len(growth)} growth + {len(dividend)} dividend = {len(stocks)} total\n\n"

    with open(path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"\n✅ Watchlist written to {path}")


def print_results(stocks: list[StockScore]) -> None:
    """Print a formatted results table."""
    print(f"\n{'='*90}")
    print(f"  RANKED UNIVERSE — {len(stocks)} Stocks")
    print(f"{'='*90}")
    print(f"  {'#':>3}  {'Symbol':8s}  {'Name':25s}  {'Score':>6}  {'Growth':>6}  {'Value':>6}  "
          f"{'OptLiq':>6}  {'Div%':>5}  {'Category':10s}")
    print(f"  {'─'*86}")

    for i, s in enumerate(stocks, 1):
        print(f"  {i:3d}  {s.symbol:8s}  {s.name[:25]:25s}  {s.composite_score:6.1f}  "
              f"{s.growth_score:6.1f}  {s.valuation_score:6.1f}  "
              f"{s.options_liquidity:6.1f}  {s.dividend_yield:5.1f}  {s.category:10s}")

    print(f"\n  Markets: ", end="")
    by_exchange = {}
    for s in stocks:
        by_exchange.setdefault(s.exchange, []).append(s.symbol)
    for exch, syms in by_exchange.items():
        print(f"{exch}({len(syms)}) ", end="")
    print()


def main():
    parser = argparse.ArgumentParser(description="Screen stocks for wheel strategy universe")
    parser.add_argument("--regions", nargs="*", default=None, help="Regions to screen: US CH JP NO AU")
    parser.add_argument("--top", type=int, default=50, help="Total stocks to select (default 50)")
    parser.add_argument("--growth", type=int, default=40, help="Growth stocks (default 40)")
    parser.add_argument("--dividend", type=int, default=10, help="Dividend stocks (default 10)")
    parser.add_argument("--min-mcap", type=float, default=5e9, help="Minimum market cap in USD (default 5B)")
    parser.add_argument("--output", type=str, default=None, help="Write watchlist YAML to this path")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="IBKR host")
    parser.add_argument("--port", type=int, default=4001, help="IBKR port")
    parser.add_argument("--client-id", type=int, default=2, help="IBKR client ID (use different from main trader)")
    args = parser.parse_args()

    print("🔍 Universe Screener — Annual Watchlist Refresh")
    print(f"   Regions: {args.regions or 'ALL'}")
    print(f"   Target: {args.growth} growth + {args.dividend} dividend = {args.top}")
    print(f"   Min market cap: ${args.min_mcap/1e9:.0f}B")
    print()

    # Connect to IBKR (use different client ID to not conflict with running trader)
    ib = IB()
    print(f"Connecting to IBKR at {args.host}:{args.port} (client {args.client_id})...")
    ib.connect(args.host, args.port, clientId=args.client_id)
    print(f"✅ Connected: {ib.managedAccounts()}")

    try:
        screener = UniverseScreener(ib)
        results = screener.screen_all(
            regions=args.regions,
            min_market_cap=args.min_mcap,
            top_n=args.top,
            growth_count=args.growth,
            dividend_count=args.dividend,
        )

        print_results(results)

        if args.output:
            generate_watchlist_yaml(results, Path(args.output))
            print(f"\n📋 Review the file at {args.output}")
            print(f"   To activate: cp {args.output} config/watchlist.yaml")
        else:
            print(f"\n💡 To save: re-run with --output config/watchlist_new.yaml")

    finally:
        ib.disconnect()
        print("\nDisconnected from IBKR.")


if __name__ == "__main__":
    main()
