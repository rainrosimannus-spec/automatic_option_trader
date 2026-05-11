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
def _load_discovered_pool() -> dict:
    """
    Load tools/discovered_pool.yaml — names added by augmentation pipeline.

    Returns dict with two keys:
      "growth": list of dicts (symbol, region, exchange, currency, added_at,
                last_made_top_60_at, source, notes)
      "dividend": list of dicts (same fields)

    Returns empty lists for both if file missing or malformed.
    Failures are non-fatal: screener falls back to CANDIDATE_POOLS only.
    """
    import os
    yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "discovered_pool.yaml")
    if not os.path.exists(yaml_path):
        return {"growth": [], "dividend": []}
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
        return {
            "growth": list(data.get("growth") or []),
            "dividend": list(data.get("dividend") or []),
        }
    except Exception:
        # Fail-safe: return empty so screener continues working
        return {"growth": [], "dividend": []}


def _load_evicted_names() -> set:
    """
    Load tools/evicted_names.yaml — symbols removed from active universe.

    Returns a set of symbol strings (just the tickers, no per-entry metadata).
    Set form is convenient for fast membership testing during universe build.

    Returns empty set if file missing or malformed. Non-fatal failure mode —
    screener continues with no evictions applied.
    """
    import os
    yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "evicted_names.yaml")
    if not os.path.exists(yaml_path):
        return set()
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
        entries = data.get("evicted") or []
        return {entry["symbol"] for entry in entries if "symbol" in entry}
    except Exception:
        return set()


def _get_growth_universe() -> dict:
    """
    Build the growth universe by merging CANDIDATE_POOLS with the
    discovered_pool["growth"] entries, then filtering out evicted symbols.

    Returns dict keyed by region. Each value: {"exchange", "currency", "symbols"}.

    Empty discovered_pool + empty evicted_names = identical to CANDIDATE_POOLS.
    """
    discovered = _load_discovered_pool()
    evicted = _load_evicted_names()

    # Start with a deep-ish copy of CANDIDATE_POOLS so we can mutate symbol lists
    merged = {}
    for region, pool in CANDIDATE_POOLS.items():
        merged[region] = {
            "exchange": pool["exchange"],
            "currency": pool["currency"],
            "symbols": [s for s in pool["symbols"] if s not in evicted],
        }
        # Preserve primary_exchange if present (used by some regions)
        if "primary_exchange" in pool:
            merged[region]["primary_exchange"] = pool["primary_exchange"]

    # Add discovered growth names. Each entry has region/exchange/currency.
    for entry in discovered.get("growth", []):
        sym = entry.get("symbol")
        region = entry.get("region")
        if not sym or not region or sym in evicted:
            continue
        if region not in merged:
            # Region from discovered_pool that's not in CANDIDATE_POOLS — create it
            merged[region] = {
                "exchange": entry.get("exchange", "SMART"),
                "currency": entry.get("currency", "USD"),
                "symbols": [],
            }
        if sym not in merged[region]["symbols"]:
            merged[region]["symbols"].append(sym)

    return merged


def _get_dividend_universe() -> dict:
    """
    Build the dividend universe by merging DIVIDEND_CANDIDATES with the
    discovered_pool["dividend"] entries, filtering out evicted symbols.
    Same shape as _get_growth_universe().
    """
    discovered = _load_discovered_pool()
    evicted = _load_evicted_names()

    merged = {}
    for region, pool in DIVIDEND_CANDIDATES.items():
        merged[region] = {
            "exchange": pool["exchange"],
            "currency": pool["currency"],
            "symbols": [s for s in pool["symbols"] if s not in evicted],
        }

    for entry in discovered.get("dividend", []):
        sym = entry.get("symbol")
        region = entry.get("region")
        if not sym or not region or sym in evicted:
            continue
        if region not in merged:
            merged[region] = {
                "exchange": entry.get("exchange", "SMART"),
                "currency": entry.get("currency", "USD"),
                "symbols": [],
            }
        if sym not in merged[region]["symbols"]:
            merged[region]["symbols"].append(sym)

    return merged


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
            "BAP",  # Credicorp — Peru bank, ~3-4% yield, decades of dividends
            "CHT",  # Chunghwa Telecom — Taiwan, ~4-5% yield, government-backed
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

def _build_breakthrough_prompt() -> str:
    """
    Build the breakthrough prompt with the current CANDIDATE_POOLS and
    DIVIDEND_CANDIDATES injected as 'do not return' exclusion list.
    Forces Claude to surface names OUTSIDE the hand-curated growth/dividend
    pool, since those names will be scored separately and don't need a
    breakthrough slot.
    """
    excluded = sorted(
        {sym for pool in _get_growth_universe().values() for sym in pool["symbols"]}
        | {sym for pool in _get_dividend_universe().values() for sym in pool["symbols"]}
    )
    excluded_str = ", ".join(excluded)
    return BREAKTHROUGH_PROMPT_TEMPLATE.format(excluded_symbols=excluded_str)


BREAKTHROUGH_PROMPT_TEMPLATE = """Generate a portfolio of up to 30 publicly listed companies for a 10-15 year
holding period, optimized to produce 2-4 names that deliver >=10x returns.
The remainder will likely underperform — that is acceptable. The goal is
portfolio shape, not 30 winners.

## ALREADY-COVERED UNIVERSE — DO NOT RETURN THESE NAMES:

The following names are already in the growth/dividend tier of our
portfolio universe. The breakthrough scan exists to find names OUTSIDE
this set. Do not return any of these:

{excluded_symbols}

If you would have selected one of these names, surface a comparable but
NOT-already-covered alternative instead.

## MEGATRENDS (assign each company to ONE primary):

1. AI/compute applications (end-user AI products, AI-enabled SaaS)
2. Compute infrastructure — the AI buildout's full stack: silicon, advanced
   packaging, networking, data center electrical and cooling, HBM/DRAM/NAND
   memory, semicap equipment, power generation for compute load. Currently the
   largest single capital-cycle in technology
3. Genomics & medical biotech (gene editing, AI drug discovery, cell therapy)
4. GLP-1 & preventive consumer health (metabolic, mental health, wearables,
   continuous monitoring, age-related joint/vision/hearing)
5. Aging populations & elder care (Japanese/European demographic crisis;
   robotics for elder care, longevity therapeutics, senior housing)
6. Energy transition (solar, wind, batteries, EVs, hydrogen)
7. Energy bridge (gas, nuclear/SMR, uranium — what powers the transition)
8. Climate adaptation (water, cooling, flood defense, drought-resistant
   agriculture, irrigation, weather modeling)
9. Critical minerals & advanced materials (lithium, rare earths, specialty
   chemicals, battery thermal management)
10. Defense & sovereignty (drones, sensors, shipbuilding, NATO/Korean/Japanese
    defense primes, supply-chain reshoring components)
11. Reindustrialization & automation (factory automation, electrical
    infrastructure, industrial REITs, specialty industrial)
12. Cybersecurity & digital sovereignty
13. Quantum computing
14. Space (launch, satellites, ground systems)
15. Nuclear fusion (early-stage, accept high mortality)
16. EM digital finance & fintech (unbanked populations coming online)
17. Frontier biotech / synthetic biology in industrial applications

## REQUIRED DISTRIBUTION:

**Megatrend spread:** Maximum 3 names per megatrend. Aim for at least 12 of
the 17 megatrends represented.

**Risk-tier barbell (this is non-negotiable — most 10x returns come from
asymmetric bets, but most asymmetric bets fail; the portfolio must have
both):**

- AT LEAST 18 names in trends already in motion (consensus-or-near-consensus
  is acceptable for these — they pay the bills and occasionally produce a
  10x via execution): aging populations, energy bridge, defense/sovereignty,
  climate adaptation, compute infrastructure, GLP-1/preventive health,
  reindustrialization, cybersecurity, EM digital finance.

- AT LEAST 6 names in genuinely speculative / contrarian megatrends where
  consensus has NOT arrived: nuclear fusion, quantum computing, true
  longevity therapeutics, room-temperature superconductors, sovereign-cloud
  infrastructure, ammonia as marine fuel, deep geothermal at tokamak scale,
  AGI agent platforms, deep-sea mineral extraction, frontier synthetic
  biology, novel space economy applications. The 6 should NOT be limited
  to these examples — surface other genuinely under-priced theses.
  Accept that 4-5 of these 6 may go to zero. The point is asymmetric upside.

- The remaining ~6 names span mid-conviction megatrends.

This barbell is the central design: most allocation in trends already in
motion (high hit-rate), but explicit allocation to speculative trends that
consensus is underweighting (where the actual 10-baggers historically hide).

**Category shape (assign each company to ONE):**
- 6 names: category-creators (companies building markets that don't yet exist)
- 6 names: incumbent-replacers (taking share in $50B+ existing TAMs)
- 6 names: picks-and-shovels (selling tools/components to whoever wins)
- 6 names: unloved sectors (boring industries with secular tailwinds —
  fertilizer, aggregates, midstream, industrial REITs, specialty chemicals)
- 6 names: underdog geographies (non-USD-currency listings — see geographic
  spread below). At least 4 names must end up in this category.

**Size distribution:**
- 12 names: $1B-$10B market cap
- 10 names: $10B-$50B market cap
- 8 names: $50B-$500B market cap

**Geographic spread:**
- 5+ names with currency != USD (count by currency, not by exchange label).
- For ANY US-listed name (NYSE/NASDAQ/AMEX/Pink), use exchange="SMART" and
  currency="USD". This is our internal convention. Do NOT return NYSE or
  NASDAQ as an exchange code.
- Among the 5+ non-USD names, include at least 3 from underweighted
  markets: Japan (TSEJ, JPY), Korea (KSE, KRW), India (NSE, INR), Brazil
  (BVMF, BRL), Israel (TASE, ILS), Eastern Europe (WSE, BUX, etc.),
  Nordics (HEX, OSE, SFB, CSE).

## EXCLUSIONS:

**HARD EXCLUSIONS — REJECT BEFORE RETURNING:**

1. The top 20 companies globally by market cap as of today. Specifically
   reject these names if considered: Apple (AAPL), Microsoft (MSFT),
   Nvidia (NVDA), Alphabet (GOOGL), Amazon (AMZN), Meta (META), Tesla
   (TSLA), Eli Lilly (LLY), Berkshire Hathaway (BRK.B), Saudi Aramco (2222),
   JPMorgan (JPM), Walmart (WMT), Visa (V), Mastercard (MA), TSMC (TSM),
   Broadcom (AVGO), ExxonMobil (XOM), Costco (COST), UnitedHealth (UNH),
   Oracle (ORCL). These cannot 10x at current cap (would exceed share of
   global GDP).

2. ETFs and funds. Reject any ticker pattern matching ETF/FND/INDEX/FUND
   in the name. Reject any company whose primary product is exposure to
   a basket of stocks. Specific examples to avoid: Defiance Quantum ETF
   (QTUM), iShares anything, Vanguard anything, ARK anything.

3. Companies whose 10x to current market cap implies >$2T at year 12.

4. Recent reverse stock splits (last 18 months).

**SOFT EXCLUSIONS:**
- Single-product biotech with binary trial dependence
- Companies with >50% revenue from a single customer

## EXISTENCE CHECK — VERIFY EACH TICKER:

For each ticker you return, verify the symbol matches a real company —
not a ticker conflation with a different company.

Examples of conflations to avoid:
- WCN is Waste Connections, NOT Welltower (Welltower is WELL).
- DHER is Delivery Hero (Frankfurt FSE), NOT John Deere (Deere is DE).
- BYD on NYSE is Boyd Gaming; the EV company BYD trades as 1211 on HKEX.
- LIN is Linde; many "lin"/"li" prefixes are unrelated.

When in doubt about a ticker, return fewer companies rather than wrong
tickers. If a name comes to mind that you suspect is private (e.g., TAE
Technologies for fusion, Stripe for payments), do NOT return it — only
public listings with verifiable tickers.

## REQUIRED OUTPUT FIELDS PER COMPANY:

JSON array. Each entry must include:

- symbol: ticker
- name: company name
- exchange: SMART for US, native exchange code for non-US (LSE, AEB,
  BVME, TSEJ, KSE, NSE, BVMF, TASE, BIT, etc.)
- currency: USD/EUR/GBP/JPY/KRW/INR/BRL/ILS as appropriate
- market_cap_usd: approximate, in billions, current
- sector: GICS-style primary sector
- megatrend: which of the 17 megatrends above (use the number + name).
  For speculative entries that don't fit the 17, use the literal name of
  the speculative trend.
- risk_tier: one of {{in_motion, mid_conviction, speculative}}
- category: one of {{category_creator, incumbent_replacer, picks_and_shovels,
  unloved_sector, underdog_geography}}
- size_bucket: one of {{early, mid, late}}
- thesis: 25-35 words. WHY this company is a 10-bagger candidate. State
  the specific mechanism (revenue growth, margin expansion, multiple
  re-rating, market share capture). Vague theses are rejected.
- mortality_risk: ONE specific failure mode in 10-15 words.
- year_4_check: 8-15 words describing what observable signal at year 4
  would confirm the thesis is on track.

## VALIDATION CHECKLIST (before returning):

IMPORTANT: if you cannot satisfy ALL items below, return your best honest
attempt with as many names as you can — do not return an empty array. A
shorter list of high-conviction names is strictly preferred to no list.

Verify all of:
- No name from the ALREADY-COVERED list above
- Each megatrend <=3 names
- Risk-tier barbell: at least 18 in_motion, at least 6 speculative
- Category split target 6/6/6/6/6 (adjust if returning fewer total)
- Size split roughly 12/10/8 (proportional if fewer total)
- 5+ names with currency != USD
- 3+ from underweighted markets
- No name in the global top 20 by market cap
- No ETFs / funds / index products
- Each ticker verified — do not return private companies or wrong-ticker
  conflations
- Each thesis is specific (mechanism stated), not vague
- Each mortality risk is specific (failure mode stated), not generic
- Mortality risks spread — no more than 5 names share the same primary
  failure mode

Return raw JSON array only. No markdown, no surrounding prose, no commentary."""


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
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": _build_breakthrough_prompt()}],
            },
            timeout=120,  # new BREAKTHROUGH_PROMPT response is ~80s
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


# ─────────────────────────────────────────────────────────────────────────
# AUGMENTATION SWAP PROPOSALS (Commit J+K — building blocks, no callers yet)
# ─────────────────────────────────────────────────────────────────────────
# Two functions that ask Claude for high-conviction company names that
# would score above the rank-60 (or rank-15 for dividend) cutoff. Used by
# the monthly augmentation pipeline (Commit L) to refresh the watchlist
# with discovered names that score better than current weakest members.

_AUGMENTATION_RUBRIC_SUMMARY = """RUBRIC (composite is weighted 25/25/20/15/15):
- revenue_durability: 5yr revenue CAGR + YoY consistency
- compounding_quality: 5yr ROIC sustained (Buffett-style compounder)
- operating_leverage: margin level + trend + growth × profitability
- innovation_investment: R&D vs sector peers + R&D growth trend
- capital_efficiency: FCF trend + share dilution + goodwill stability
STRUCTURAL CAP: score floored at 30 if 3+ years negative net income AND
3+ years negative FCF in the 5-year window."""


def _format_score_table_for_prompt(scores: list, max_rows: int = 60) -> str:
    """Format a list of StockScore objects as a compact prompt table.
    Shows: rank, symbol, composite, then 5 sub-scores."""
    lines = ["  rank symbol     composite  rev cmp op rd cap"]
    for i, s in enumerate(scores[:max_rows], 1):
        # Sub-scores stored on StockScore by _score_stock (Commit O)
        rev = getattr(s, "sub_revenue_durability", 0) or 0
        cmp_ = getattr(s, "sub_compounding_quality", 0) or 0
        op = getattr(s, "sub_operating_leverage", 0) or 0
        rd = getattr(s, "sub_innovation_investment", 0) or 0
        cap = getattr(s, "sub_capital_efficiency", 0) or 0
        composite = s.forward_growth_score or 0
        lines.append(
            f"  {i:3d}. {s.symbol:8s}  {composite:6.1f}    {rev:3.0f} {cmp_:3.0f} {op:3.0f} {rd:3.0f} {cap:3.0f}"
        )
    return "\n".join(lines)


def _build_growth_augmentation_prompt(
    top_60: list,
    ranks_61_to_120: list,
    cutoff_score: float,
    exclusion_set: set,
) -> str:
    """Build the augmentation prompt for growth tier."""
    excluded_str = ", ".join(sorted(exclusion_set)) if exclusion_set else "(none)"

    return f"""You're evaluating long-term compounder candidates for a curated watchlist.

CURRENT TOP-60 GROWTH (sub-scores: rev=revenue_durability, cmp=compounding_quality,
op=operating_leverage, rd=innovation_investment, cap=capital_efficiency):
{_format_score_table_for_prompt(top_60, max_rows=60)}

NAMES JUST BELOW (rank 61-120, for context — these are the names your proposals
would compete against if any of them entered the universe):
{_format_score_table_for_prompt(ranks_61_to_120, max_rows=60)}

EXCLUDED — already in our universe, do NOT propose:
{excluded_str}

{_AUGMENTATION_RUBRIC_SUMMARY}

YOUR TASK:
Propose 5-10 high-conviction company names that would likely score ABOVE {cutoff_score:.1f}
(our current rank-60 cutoff) under this rubric. Focus on:
- Sustained high ROIC (15%+ over 5 years)
- Profitable AND growing (positive operating margin, expanding)
- Smart capital allocation (low share dilution, no goodwill bloat from M&A)
- Investing in innovation (R&D appropriate to sector)

Avoid:
- Mega-caps already in our universe (see EXCLUDED list)
- Speculative/unprofitable names (those belong to breakthrough tier, not growth)
- Names you're not confident actually exist with the ticker you provide

OUTPUT FORMAT — strict JSON array, no other text, no markdown fencing:
[
  {{"symbol": "ABC", "exchange": "SMART", "currency": "USD", "region": "US",
    "thesis": "2-3 sentence thesis grounded in the 5 components — explain why this would beat {cutoff_score:.1f}"}},
  ...
]"""


def _build_dividend_augmentation_prompt(
    top_15: list,
    ranks_16_to_45: list,
    cutoff_score: float,
    exclusion_set: set,
) -> str:
    """Build the augmentation prompt for dividend tier.
    Different rubric — dividend uses dividend_total_return_score, not forward_growth_score."""
    excluded_str = ", ".join(sorted(exclusion_set)) if exclusion_set else "(none)"

    def fmt_div(scores, max_rows):
        lines = ["  rank symbol     div_score  yield  cagr  payout"]
        for i, s in enumerate(scores[:max_rows], 1):
            score = s.dividend_total_return_score or 0
            yld = s.dividend_yield or 0
            cagr = getattr(s, "_dividend_cagr_5yr", None) or 0
            payout = getattr(s, "_payout_ratio", None) or 0
            lines.append(
                f"  {i:3d}. {s.symbol:8s}  {score:6.1f}    {yld:5.2f}% {cagr:5.1f}% {payout:5.1f}%"
            )
        return "\n".join(lines)

    return f"""You're evaluating dividend-paying companies for a curated 10-year total return watchlist.

CURRENT TOP-15 DIVIDEND:
{fmt_div(top_15, 15)}

NAMES JUST BELOW (rank 16-45, for context):
{fmt_div(ranks_16_to_45, 30)}

EXCLUDED — already in our universe, do NOT propose:
{excluded_str}

DIVIDEND RUBRIC: Total return = dividends received + price appreciation.
Components scored 0-100, weighted: 35% dividend CAGR (the most important factor
for 10yr return), 25% current yield (sweet spot 2.5-6%, above 8% is yield-trap
territory), 20% payout sustainability (below 70% is healthy), 20% revenue+FCF
trend as price appreciation proxy.

YOUR TASK:
Propose 5-10 high-conviction dividend names that would likely score ABOVE {cutoff_score:.1f}
(our current rank-15 cutoff). Focus on:
- Strong dividend CAGR (8%+ over 5 years preferred — beats inflation + grows wealth)
- Sustainable payout (below 70% of earnings, room to keep growing)
- Yield in the 2.5-6% sweet spot (avoid yield traps above 8%)
- Stable or growing revenue (not declining businesses with high yields)
- Geographic diversity welcome (LSE, Nordic, Asian dividend payers)

Avoid:
- Names already in our universe (see EXCLUDED)
- Yield traps (>8% yield with declining revenue/earnings)
- Recent dividend cutters

OUTPUT FORMAT — strict JSON array, no other text, no markdown fencing:
[
  {{"symbol": "ABC", "exchange": "SMART", "currency": "USD", "region": "US_DIV",
    "thesis": "2-3 sentence thesis: dividend CAGR, sustainability, yield context"}},
  ...
]"""


def _call_claude_for_swaps(prompt: str, label: str) -> list[dict]:
    """Shared helper: send prompt to Claude, parse JSON array, return list of dicts.
    Empty list on failure. Matches _get_breakthrough_candidates pattern."""
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
                "max_tokens": 4000,  # smaller than breakthrough — only 5-10 names
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        proposals = json.loads(text.strip())
        if not isinstance(proposals, list):
            print(f"  ❌ {label} swap proposals: response was not a JSON array")
            return []
        print(f"  ✅ Claude returned {len(proposals)} {label} swap proposals")
        return proposals
    except Exception as e:
        print(f"  ❌ {label} swap proposals failed: {e}")
        return []


def _get_growth_swaps(
    top_60: list,
    ranks_61_to_120: list,
    cutoff_score: float,
    exclusion_set: set,
) -> list[dict]:
    """Ask Claude for 5-10 growth-tier swap candidates that would beat the rank-60 cutoff."""
    prompt = _build_growth_augmentation_prompt(top_60, ranks_61_to_120, cutoff_score, exclusion_set)
    return _call_claude_for_swaps(prompt, "growth")


def _get_dividend_swaps(
    top_15: list,
    ranks_16_to_45: list,
    cutoff_score: float,
    exclusion_set: set,
) -> list[dict]:
    """Ask Claude for 5-10 dividend-tier swap candidates that would beat the rank-15 cutoff."""
    prompt = _build_dividend_augmentation_prompt(top_15, ranks_16_to_45, cutoff_score, exclusion_set)
    return _call_claude_for_swaps(prompt, "dividend")


# ─────────────────────────────────────────────────────────────────────────
# BREAKTHROUGH HISTORY (May 10 — Step B persistence; eviction stubbed)
# ─────────────────────────────────────────────────────────────────────────
# Tracks names that pass _check_breakthrough_eligibility across runs.
# Each entry: appearance_count + first_seen + last_seen (calendar-month YYYY-MM).
# Stored as the `breakthrough:` key in tools/discovered_pool.yaml alongside
# growth/dividend (which augmentation populates).
#
# Insert/update logic (this commit): on each eligible breakthrough candidate,
#   - new symbol → insert with count=1, first_seen=last_seen=current_month
#   - existing symbol → bump count, update last_seen, refresh thesis_latest
#
# Eviction logic (TODO — next session):
#   protect last 6 calendar months; beyond window, drop lowest count first,
#   tiebreak by oldest last_seen, until pool <= 75. Pool may exceed 75
#   transiently when all names are within protection window.
BREAKTHROUGH_HISTORY_CAP = 75


def _persist_breakthrough_appearance(
    symbol: str,
    exchange: str,
    currency: str,
    name: str,
    megatrend: str,
    thesis: str,
    run_date,
) -> bool:
    """
    Insert-or-update a breakthrough name into discovered_pool.yaml.

    Calendar-month bucketing: first_seen / last_seen are YYYY-MM strings.
    Multiple manual runs in the same calendar month do NOT bump the count —
    a name appearing in 3 runs in May counts as 1 May appearance.

    Atomic write via .tmp + os.replace (matches _persist_augmentation_acceptances).
    Returns True on success, False on any error (logged but not raised —
    breakthrough persistence is best-effort, scan should still complete).
    """
    import os
    import yaml

    yaml_path = os.path.join(os.path.dirname(__file__), "discovered_pool.yaml")
    tmp_path = yaml_path + ".tmp"

    # Calendar-month bucket: format run_date as YYYY-MM
    # ISO timestamp: full run_date as ISO 8601 string — identifies the exact run
    try:
        if hasattr(run_date, "strftime"):
            run_month = run_date.strftime("%Y-%m")
            run_iso = run_date.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            # Defensive — if run_date is a string, take first 7 chars (YYYY-MM)
            run_month = str(run_date)[:7]
            run_iso = str(run_date)[:19]
    except Exception as e:
        print(f"  ⚠ breakthrough persist: bad run_date {run_date!r}: {e}")
        return False

    try:
        if os.path.exists(yaml_path):
            with open(yaml_path) as f:
                existing = yaml.safe_load(f) or {}
        else:
            existing = {}

        if "breakthrough" not in existing or existing["breakthrough"] is None:
            existing["breakthrough"] = []
        elif not isinstance(existing["breakthrough"], list):
            print(f"  ⚠ discovered_pool.yaml[breakthrough] was not a list, resetting")
            existing["breakthrough"] = []

        # Find existing entry by symbol
        match = None
        for entry in existing["breakthrough"]:
            if entry.get("symbol") == symbol:
                match = entry
                break

        if match is None:
            # New name — insert
            existing["breakthrough"].append({
                "symbol": symbol,
                "exchange": exchange,
                "currency": currency,
                "name": name,
                "megatrend": megatrend,
                "thesis_latest": (thesis or "")[:400],
                "first_seen": run_month,
                "last_seen": run_month,
                "last_run_at": run_iso,
                "appearance_count": 1,
            })
            print(f"  📒  breakthrough_history: NEW {symbol} ({run_month})")
        else:
            # Existing — only bump if this is a different month than last_seen
            if match.get("last_seen") != run_month:
                match["appearance_count"] = (match.get("appearance_count") or 0) + 1
                match["last_seen"] = run_month
                print(f"  📒  breakthrough_history: BUMP {symbol} count={match['appearance_count']} ({run_month})")
            else:
                # Same month, already counted — just refresh thesis & metadata
                pass
            # Always refresh thesis and run-cursor to latest version
            match["thesis_latest"] = (thesis or match.get("thesis_latest", ""))[:400]
            match["megatrend"] = megatrend or match.get("megatrend", "")
            match["name"] = name or match.get("name", symbol)
            match["last_run_at"] = run_iso

        with open(tmp_path, "w") as f:
            yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp_path, yaml_path)
        return True

    except Exception as e:
        print(f"  ❌  breakthrough_history persist failed for {symbol}: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _evict_breakthrough_overflow() -> bool:
    """
    Trim discovered_pool.yaml[breakthrough] when it exceeds BREAKTHROUGH_HISTORY_CAP,
    while protecting names within a 6-calendar-month recency window.

    Locked design (Sunday 2026-05-10):
    - Protection window: last 6 calendar months of last_seen, inclusive of the
      current month. Names within window are NEVER evicted.
    - Beyond the window, sort eviction-eligible names by:
        (appearance_count ASC, last_seen ASC)
      and remove from the front until len(pool) <= BREAKTHROUGH_HISTORY_CAP.
    - If all names are within protection window and pool > cap, allow transient
      overflow — recency dominates strict cap.

    Atomic write via .tmp + os.replace, mirroring _persist_breakthrough_appearance.

    Returns True on success or no-op, False on any error (logged but not raised —
    eviction is best-effort, scan should still complete).
    """
    import os
    from datetime import date
    import yaml

    yaml_path = os.path.join(os.path.dirname(__file__), "discovered_pool.yaml")
    tmp_path = yaml_path + ".tmp"

    if not os.path.exists(yaml_path):
        return True  # nothing to evict

    try:
        with open(yaml_path) as f:
            existing = yaml.safe_load(f) or {}

        pool = existing.get("breakthrough") or []
        if not isinstance(pool, list):
            print(f"  ⚠  breakthrough_history evict: pool not a list, skipping")
            return False

        if len(pool) <= BREAKTHROUGH_HISTORY_CAP:
            return True  # no-op, under cap

        # Build protection-window cutoff: 6 calendar months back from today,
        # inclusive of current month. E.g. today=2026-05-XX → cutoff=2025-12.
        # A name with last_seen >= cutoff is protected.
        today = date.today()
        # Compute year/month 6 months ago (5 months back from current month = window
        # of 6 calendar months total, current month inclusive).
        cutoff_total_months = today.year * 12 + (today.month - 1) - 5
        cutoff_year = cutoff_total_months // 12
        cutoff_month = (cutoff_total_months % 12) + 1
        cutoff_yyyymm = f"{cutoff_year:04d}-{cutoff_month:02d}"

        protected = []
        evictable = []
        for entry in pool:
            ls = (entry.get("last_seen") or "")[:7]
            if ls >= cutoff_yyyymm:
                protected.append(entry)
            else:
                evictable.append(entry)

        # Sort evictable: lowest appearance_count first, then oldest last_seen
        # as tiebreaker. Ties beyond that: stable sort preserves input order.
        evictable.sort(key=lambda e: (
            int(e.get("appearance_count") or 0),
            (e.get("last_seen") or ""),
        ))

        # Evict from the front of evictable until total len <= cap, but
        # never touch protected. If protected alone exceeds cap, all
        # evictables stay too (transient overflow).
        target_total = BREAKTHROUGH_HISTORY_CAP
        n_protected = len(protected)
        n_keep_evictable = max(0, target_total - n_protected)
        evicted = evictable[: max(0, len(evictable) - n_keep_evictable)]
        kept_evictable = evictable[max(0, len(evictable) - n_keep_evictable):]

        new_pool = protected + kept_evictable

        if not evicted:
            # Above cap but everyone is protected — transient overflow.
            print(f"  📒  breakthrough_history: {len(pool)} > cap {BREAKTHROUGH_HISTORY_CAP}, "
                  f"all within {cutoff_yyyymm} window — transient overflow allowed")
            return True

        existing["breakthrough"] = new_pool

        with open(tmp_path, "w") as f:
            yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp_path, yaml_path)

        evicted_syms = ", ".join(e.get("symbol", "?") for e in evicted[:10])
        if len(evicted) > 10:
            evicted_syms += f", ... +{len(evicted) - 10} more"
        print(f"  📒  breakthrough_history: evicted {len(evicted)} "
              f"(pool {len(pool)} → {len(new_pool)}, cutoff {cutoff_yyyymm}): {evicted_syms}")
        return True

    except Exception as e:
        print(f"  ❌  breakthrough_history evict failed: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────
# BREAKTHROUGH SELECTION PROMPT (Big-Ticket #2, Commit 3 of 4)
# ─────────────────────────────────────────────────────────────────────────
# Call B of the anchored 30->25 selection. Given fresh proposals (call A
# output) and the prior-run anchor (entries with max last_run_at), build a
# prompt that asks Claude to select 25 with reasoning.


def _build_breakthrough_selection_prompt(fresh: list[dict], anchor: list[dict]) -> str:
    """
    Build the call-B prompt for anchored breakthrough selection.

    Inputs:
        fresh:  list of dicts from call A. Each must have 'symbol',
                'megatrend', 'thesis' (or 'rationale') keys. Up to ~30.
        anchor: list of dicts from discovered_pool.yaml[breakthrough]
                where last_run_at == max(last_run_at). Each has 'symbol',
                'megatrend', 'thesis_latest' keys. Up to 25.

    Output: prompt string. No API call.

    Overlap handling: names appearing in both fresh and anchor are labeled
    'both' in the merged list — Claude sees the dual provenance and can
    use it as a conviction signal without seeing the raw appearance_count.

    Bootstrap: if anchor is empty (first run of new design), the prompt
    drops the anchor section and tells Claude there is no prior selection
    to anchor against.
    """
    # Build symbol -> source label map: 'fresh', 'anchor', or 'both'
    fresh_syms = {e.get("symbol") for e in fresh if e.get("symbol")}
    anchor_syms = {e.get("symbol") for e in anchor if e.get("symbol")}
    both = fresh_syms & anchor_syms

    # Merge: anchor first (preserves their full thesis_latest), then
    # fresh-only names. Dedup on symbol.
    merged = []
    seen = set()
    for e in anchor:
        sym = e.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        label = "both" if sym in both else "anchor"
        merged.append({
            "symbol": sym,
            "megatrend": e.get("megatrend", "uncategorized"),
            "thesis": (e.get("thesis_latest") or "")[:400],
            "source": label,
        })
    for e in fresh:
        sym = e.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        merged.append({
            "symbol": sym,
            "megatrend": e.get("megatrend", "uncategorized"),
            "thesis": (e.get("thesis") or e.get("rationale") or "")[:400],
            "source": "fresh",
        })

    n_total = len(merged)
    n_anchor = len(anchor_syms)
    n_fresh = len(fresh_syms)
    n_both = len(both)

    # Build the candidate listing
    if merged:
        listing_lines = []
        for c in merged:
            listing_lines.append(
                f"- {c['symbol']} [{c['source']}] | megatrend: {c['megatrend']}\n"
                f"  thesis: {c['thesis']}"
            )
        candidates_block = "\n".join(listing_lines)
    else:
        candidates_block = "(no candidates available)"

    # Bootstrap note
    bootstrap_note = ""
    if not anchor:
        bootstrap_note = (
            "\n\nNOTE: This is the first run of the anchored-selection design. "
            "There is no prior selection to anchor against. Select 25 from the "
            "fresh candidates alone using the rubric below.\n"
        )

    prompt = f"""You are selecting the final breakthrough portfolio slate for a 10-15 year holding horizon.

You will be given a merged pool of candidates from two sources:
- ANCHOR: names selected on the previous screener run (high conviction by prior judgment)
- FRESH: names freshly proposed by the current run's breakthrough scan
- BOTH: names appearing in both sources (strongest dual signal)

Your job: pick exactly 25 names from this pool with one-sentence reasoning each, plus a short group-level reasoning paragraph.{bootstrap_note}

POOL SIZE: {n_total} unique candidates ({n_anchor} from anchor, {n_fresh} from fresh, {n_both} overlap labeled BOTH).

SELECTION RUBRIC (balanced):
1. Thesis strength — is the 10-15 year compounding case real and durable?
2. Megatrend diversity — the final 25 should span multiple megatrends, not pile into one or two
3. Conviction signals — names labeled BOTH carry dual provenance; treat that as positive signal but do not rely on it alone (a thesis-weak BOTH name should still lose to a thesis-strong FRESH or ANCHOR name)

SAFETY NET: if you cannot honestly find 25 strong picks, return your best attempt with as many as you can — do not return an empty array. A shorter list of high-conviction names is strictly preferred to forcing weak picks to hit 25.

CANDIDATES:
{candidates_block}

OUTPUT FORMAT:
Return ONLY valid JSON, no markdown, no prose outside JSON, no code fences.
Schema:
{{
  "selected": [
    {{"symbol": "SYM1", "reasoning": "one sentence why this earned the slot"}},
    ...
  ],
  "group_reasoning": "<=200 word paragraph covering: thesis clusters represented, notable inclusions/exclusions, megatrend distribution"
}}

Per-pick reasoning rules:
- One sentence each, factual not promotional
- Reference the rubric: which of thesis/diversity/conviction drove this pick
- No marketing language ('exciting', 'revolutionary', 'game-changer')

Return the JSON object now."""

    return prompt


# ─────────────────────────────────────────────────────────────────────────
# AUGMENTATION ORCHESTRATION (Commit L)
# ─────────────────────────────────────────────────────────────────────────
# AUGMENTATION_ENABLED toggles Phase 2.5 in screen_all().
# Default OFF — must be manually flipped to True to activate.
# When OFF, augmentation code is bypassed entirely (no API calls, no DB writes).
AUGMENTATION_ENABLED = True


def _persist_augmentation_acceptances(additions: dict) -> bool:
    """
    Append accepted augmentation proposals to tools/discovered_pool.yaml.
    
    Schema of additions:
        {
            "growth": [
                {"symbol", "exchange", "currency", "region", "score", "added_date", "thesis"},
                ...
            ],
            "dividend": [...]
        }
    
    Atomicity: writes to .tmp file then renames. If interrupted, original yaml
    is preserved.
    
    Returns True on success, False on any error (logged but not raised — augmentation
    is best-effort).
    """
    if not additions or (not additions.get("growth") and not additions.get("dividend")):
        return True  # nothing to persist
    
    import os
    import yaml
    
    yaml_path = os.path.join(os.path.dirname(__file__), "discovered_pool.yaml")
    tmp_path = yaml_path + ".tmp"
    
    try:
        if os.path.exists(yaml_path):
            with open(yaml_path) as f:
                existing = yaml.safe_load(f) or {}
        else:
            existing = {}
        
        for tier in ("growth", "dividend"):
            if tier not in existing or existing[tier] is None:
                existing[tier] = []
            elif not isinstance(existing[tier], list):
                print(f"  ⚠ discovered_pool.yaml[{tier}] was not a list, resetting to []")
                existing[tier] = []
        
        for tier, new_entries in additions.items():
            if tier not in ("growth", "dividend"):
                continue
            for entry in new_entries:
                if any(e.get("symbol") == entry["symbol"] for e in existing[tier]):
                    print(f"  ⚠ {entry['symbol']} already in discovered_pool[{tier}], skipping")
                    continue
                existing[tier].append(entry)
        
        with open(tmp_path, "w") as f:
            yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp_path, yaml_path)
        
        n_growth = len(additions.get("growth", []))
        n_div = len(additions.get("dividend", []))
        print(f"  💾 Persisted to discovered_pool.yaml: +{n_growth} growth, +{n_div} dividend")
        return True
    
    except Exception as e:
        print(f"  ❌ Failed to persist discovered_pool.yaml: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────
# DISCOVERED POOL EVICTION (Commit M)
# ─────────────────────────────────────────────────────────────────────────
# Caps prevent unlimited growth of discovered_pool.yaml. When a tier exceeds
# its cap, the lowest-scored entries are evicted (kept: highest-scored cap names).
# Eviction is list-size hygiene only — NOT a quality verdict.
DISCOVERED_POOL_CAP_GROWTH = 180
DISCOVERED_POOL_CAP_DIVIDEND = 45


def _evict_overflow_from_discovered_pool() -> bool:
    """
    Trim discovered_pool.yaml entries beyond cap by lowest score.
    
    For each tier:
      - If len(pool) > cap: sort by score desc, keep [:cap], drop the rest
      - Log evicted symbols
    
    Atomic write via .tmp+rename (same pattern as _persist).
    Returns True on success or no-op, False on error.
    """
    import os
    import yaml
    
    yaml_path = os.path.join(os.path.dirname(__file__), "discovered_pool.yaml")
    tmp_path = yaml_path + ".tmp"
    
    if not os.path.exists(yaml_path):
        return True  # nothing to evict from
    
    try:
        with open(yaml_path) as f:
            existing = yaml.safe_load(f) or {}
        
        caps = {
            "growth": DISCOVERED_POOL_CAP_GROWTH,
            "dividend": DISCOVERED_POOL_CAP_DIVIDEND,
        }
        any_evicted = False
        
        for tier, cap in caps.items():
            pool = existing.get(tier) or []
            if not isinstance(pool, list) or len(pool) <= cap:
                continue
            
            # Sort by score descending (defaults to 0 if missing)
            pool_sorted = sorted(pool, key=lambda e: e.get("score", 0) or 0, reverse=True)
            keep = pool_sorted[:cap]
            evicted = pool_sorted[cap:]
            
            evicted_summary = ", ".join(
                f"{e.get('symbol', '?')}({e.get('score', 0):.1f})" for e in evicted[:10]
            )
            if len(evicted) > 10:
                evicted_summary += f" ... +{len(evicted) - 10} more"
            
            print(f"  🧹 Evicting {len(evicted)} from {tier} pool (was {len(pool)}, cap {cap}): {evicted_summary}")
            existing[tier] = keep
            any_evicted = True
        
        if not any_evicted:
            return True  # all pools within cap, nothing to do
        
        # Atomic write
        with open(tmp_path, "w") as f:
            yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp_path, yaml_path)
        return True
    
    except Exception as e:
        print(f"  ❌ Failed to evict from discovered_pool.yaml: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _process_augmentation_proposal(
    proposal: dict,
    tier: str,
    cutoff_score: float,
    all_scores: list,
    audit_session,
    run_date,
    pending_additions: dict = None,
    screener=None,
) -> bool:
    """
    Process a single Claude swap proposal.
    
    Steps:
    1. Score the proposed symbol via _score_stock (real composite score)
    2. Compare against cutoff_score (margin = 0)
    3. If beats cutoff: append to all_scores, audit as accepted
    4. If not: audit as rejected
    
    Returns True if accepted, False if rejected.
    Failures (FMP missing, scoring exception) → audit as rejected with reason.
    """
    # Lazy import — matches codebase pattern (see _get_breakthrough_candidates for get_settings)
    from src.portfolio.models import AugmentationAudit
    
    symbol = proposal.get("symbol", "?")
    exchange = proposal.get("exchange", "SMART")
    currency = proposal.get("currency", "USD")
    region = proposal.get("region", tier.upper())
    thesis = proposal.get("thesis", "")
    
    # Defensive: skip if symbol already in all_scores
    if any(s.symbol == symbol for s in all_scores):
        audit_row = AugmentationAudit(
            run_date=run_date, tier=tier,
            proposed_symbol=symbol, proposed_score=0.0, cutoff_score=cutoff_score,
            displaced_symbol=None, displaced_score=None,
            accepted=False, reason="duplicate",
            notes=f"already in all_scores; thesis was: {thesis[:200]}",
        )
        audit_session.add(audit_row)
        return False
    
    # Score it
    try:
        if screener is None:
            raise RuntimeError("_process_augmentation_proposal requires screener=<UniverseScreener instance>")
        score = screener._score_stock(symbol, exchange=exchange, currency=currency)
        if score is None:
            raise ValueError("_score_stock returned None")
    except Exception as e:
        audit_row = AugmentationAudit(
            run_date=run_date, tier=tier,
            proposed_symbol=symbol, proposed_score=0.0, cutoff_score=cutoff_score,
            displaced_symbol=None, displaced_score=None,
            accepted=False, reason="scoring_failed",
            notes=f"{type(e).__name__}: {str(e)[:200]} | thesis: {thesis[:200]}",
        )
        audit_session.add(audit_row)
        return False
    
    # Pick the comparison metric based on tier
    if tier == "growth":
        proposal_score = score.forward_growth_score or 0.0
    else:  # dividend
        proposal_score = score.dividend_total_return_score or 0.0
    
    # Acceptance check (margin = 0)
    if proposal_score > cutoff_score:
        score.tier = tier
        all_scores.append(score)
        audit_row = AugmentationAudit(
            run_date=run_date, tier=tier,
            proposed_symbol=symbol, proposed_score=proposal_score, cutoff_score=cutoff_score,
            displaced_symbol=None, displaced_score=None,
            accepted=True, reason="beat_cutoff",
            notes=f"thesis: {thesis[:300]}",
        )
        audit_session.add(audit_row)
        # Buffer for yaml persistence (if caller passed a buffer)
        if pending_additions is not None:
            pending_additions.setdefault(tier, []).append({
                "symbol": symbol,
                "exchange": exchange,
                "currency": currency,
                "region": region,
                "score": float(proposal_score),
                "added_date": run_date.strftime("%Y-%m-%d"),
                "thesis": thesis[:300],
            })
        print(f"  ✅ ACCEPTED  {symbol:8s} {tier:8s} score={proposal_score:.1f} > cutoff={cutoff_score:.1f}")
        return True
    else:
        audit_row = AugmentationAudit(
            run_date=run_date, tier=tier,
            proposed_symbol=symbol, proposed_score=proposal_score, cutoff_score=cutoff_score,
            displaced_symbol=None, displaced_score=None,
            accepted=False, reason="below_cutoff",
            notes=f"thesis: {thesis[:300]}",
        )
        audit_session.add(audit_row)
        print(f"  ❌ REJECTED  {symbol:8s} {tier:8s} score={proposal_score:.1f} <= cutoff={cutoff_score:.1f}")
        return False



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
    Plus 5-year history for forward-growth scorer (ROE, payout, operating margin,
    R&D intensity, dilution, goodwill, neg net income years).
    Uses 4-5 API calls — monthly screener only.
    """
    result = {}

    # ── Income statement: revenue, margins, R&D, dilution, neg-NI count ──
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
                result["gross_margin_trend"] = gm_new - gm_old
            # ── NEW: Operating margin (level + 3yr trend) ──
            op_income_latest = income[0].get("operatingIncome", 0)
            if revenue and revenue > 0:
                result["operating_margin_pct"] = op_income_latest / revenue * 100
            if len(income) >= 3:
                rev_3yr = income[2].get("revenue", 0)
                op_3yr = income[2].get("operatingIncome", 0)
                if rev_3yr and revenue:
                    om_old = (op_3yr / rev_3yr) * 100
                    om_new = (op_income_latest / revenue) * 100
                    result["operating_margin_trend"] = om_new - om_old
            # ── NEW: R&D intensity (level + 3yr trend) ──
            # Field name in FMP: researchAndDevelopmentExpenses
            rd_latest = income[0].get("researchAndDevelopmentExpenses", 0) or 0
            if revenue and revenue > 0:
                result["rd_intensity_pct"] = (rd_latest / revenue) * 100
            if len(income) >= 3:
                rd_3yr = income[2].get("researchAndDevelopmentExpenses", 0) or 0
                rev_3yr = income[2].get("revenue", 0) or 0
                if rev_3yr and revenue:
                    rd_old_pct = (rd_3yr / rev_3yr) * 100 if rev_3yr else 0
                    rd_new_pct = (rd_latest / revenue) * 100
                    result["rd_intensity_trend"] = rd_new_pct - rd_old_pct
            # ── NEW: Share dilution (3yr) ──
            # Positive = dilution, negative = buybacks
            shs_latest = income[0].get("weightedAverageShsOutDil", 0) or 0
            if len(income) >= 3:
                shs_3yr = income[2].get("weightedAverageShsOutDil", 0) or 0
                if shs_3yr and shs_latest:
                    result["share_dilution_pct_3yr"] = ((shs_latest - shs_3yr) / abs(shs_3yr)) * 100
            # ── NEW: Negative net income years over 5-year window ──
            result["net_income_negative_years_5yr"] = sum(
                1 for i in income if (i.get("netIncome") or 0) < 0
            )
        except Exception:
            pass

    # ── Ratios: PE, PEG, dividend yield, payout ──
    ratios = _fmp_get("ratios", symbol, {"limit": 5})
    if ratios and len(ratios) >= 1:
        try:
            r = ratios[0]
            result["pe_ratio"] = (r.get("priceToEarningsRatio")
                                  or r.get("priceEarningsRatio") or 0)
            result["peg_ratio"] = (r.get("priceToEarningsGrowthRatio")
                                   or r.get("priceEarningsToGrowthRatio") or 0)
            result["payout_ratio"] = ((r.get("dividendPayoutRatio")
                                       or r.get("payoutRatio") or 0) * 100)
            result["dividend_yield"] = (r.get("dividendYield") or 0) * 100
            result["roe"] = (r.get("returnOnEquity") or 0) * 100
            if len(ratios) >= 2:
                prev_yield = (ratios[1].get("dividendYield") or 0) * 100
                curr_yield = result["dividend_yield"]
                result["dividend_cut"] = (prev_yield > 0.5 and curr_yield < prev_yield * 0.7)
            else:
                result["dividend_cut"] = False
            # ── NEW: 5-year payout history (avg) — deposit for future dividend tier ──
            payout_history = []
            for entry in ratios:
                v = entry.get("dividendPayoutRatio") or entry.get("payoutRatio")
                if v is not None:
                    payout_history.append(v)
            if payout_history:
                result["payout_ratio_5yr_avg"] = sum(payout_history) / len(payout_history)
            else:
                result["payout_ratio_5yr_avg"] = 0  # treat unknown as no-payout
        except Exception:
            pass

    # ── Key metrics: ROE history (5 years) + payout history ──
    # BUMPED limit 1 → 5 to support compounding-quality scoring
    km = _fmp_get("key-metrics", symbol, {"limit": 5})
    if km and len(km) >= 1:
        try:
            # Latest ROE — overrides /ratios fallback
            roe_raw = km[0].get("returnOnEquity")
            if roe_raw is not None:
                result["roe"] = roe_raw * 100
            # ── NEW: 5-year ROE history (avg + min) ──
            roe_history = []
            for entry in km:
                v = entry.get("returnOnEquity")
                if v is not None:
                    roe_history.append(v * 100)
            if roe_history:
                result["roe_5yr_avg"] = sum(roe_history) / len(roe_history)
                result["roe_5yr_min"] = min(roe_history)
            # ── NEW: 5-year ROIC history (avg + min) ──
            # ROIC is the right compounding signal for growth-tier companies.
            # ROE × retention works for mature dividend payers but collapses to ROE
            # for growth names that don't pay dividends. ROIC measures returns on
            # ALL deployed capital (debt + equity) — what matters for compounders.
            roic_history = []
            for entry in km:
                v = entry.get("returnOnInvestedCapital")
                if v is not None:
                    roic_history.append(v * 100)
            if roic_history:
                result["roic_5yr_avg"] = sum(roic_history) / len(roic_history)
                result["roic_5yr_min"] = min(roic_history)
            # ── NEW: Compounding quality raw = ROIC 5yr avg ──
            # A company sustaining 20%+ ROIC for 5 years is genuinely compounding
            # capital. < 10% sustained = capital being deployed inefficiently.
            if roic_history:
                result["compounding_quality_raw"] = result["roic_5yr_avg"]
        except Exception:
            pass

    # ── Balance sheet: debt + goodwill trend ──
    # BUMPED limit 2 → 5 to support goodwill-stability scoring
    bs = _fmp_get("balance-sheet-statement", symbol, {"limit": 5})
    if bs and len(bs) >= 1:
        try:
            total_debt = bs[0].get("totalDebt") or 0
            equity = bs[0].get("totalStockholdersEquity") or 1
            result["debt_to_equity"] = total_debt / abs(equity) if equity else 0
            # ── NEW: Goodwill / total assets, now and 5 years ago ──
            # Captures whether company has been making big acquisitions (goodwill
            # bloat) or growing organically. Stable goodwill = no destructive M&A.
            gw_now = bs[0].get("goodwillAndIntangibleAssets") or bs[0].get("goodwill") or 0
            assets_now = bs[0].get("totalAssets") or 1
            if assets_now and assets_now > 0:
                result["goodwill_to_assets_now_pct"] = (gw_now / assets_now) * 100
            if len(bs) >= 5:
                gw_old = bs[4].get("goodwillAndIntangibleAssets") or bs[4].get("goodwill") or 0
                assets_old = bs[4].get("totalAssets") or 1
                if assets_old and assets_old > 0:
                    result["goodwill_to_assets_5yr_ago_pct"] = (gw_old / assets_old) * 100
                if "goodwill_to_assets_now_pct" in result and "goodwill_to_assets_5yr_ago_pct" in result:
                    result["goodwill_to_assets_change_pct"] = (
                        result["goodwill_to_assets_now_pct"] - result["goodwill_to_assets_5yr_ago_pct"]
                    )
        except Exception:
            pass

    # ── Cash flow: FCF quality and trend ──
    # BUMPED limit 4 → 5 for symmetry with structural-quality 5yr window
    cf = _fmp_get("cash-flow-statement", symbol, {"limit": 5})
    if cf:
        try:
            # Existing field — keep for backward compat
            result["fcf_negative_years"] = sum(
                1 for c in cf if (c.get("freeCashFlow") or 0) < 0
            )
            # ── NEW: explicit 5-year window count for structural-quality cap ──
            result["fcf_negative_years_5yr"] = result["fcf_negative_years"]
            result["fcf_latest"] = cf[0].get("freeCashFlow") or 0
            # FCF margin trend: is FCF growing as % of revenue?
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

    # ── Dividend CAGR: most important factor for 10-year dividend total return ──
    div_yield = result.get("dividend_yield", 0)
    if div_yield > 0.5:
        try:
            hist = _fmp_get("historical-dividends", symbol)
            if hist and isinstance(hist, dict):
                payments = hist.get("historical", [])
            elif hist and isinstance(hist, list):
                payments = hist
            else:
                payments = []
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
                d_new = by_year[years_sorted[0]]
                d_3yr = by_year[years_sorted[3]]
                if d_new > 0 and d_3yr > 0:
                    cagr_3yr = ((d_new / d_3yr) ** (1/3) - 1) * 100
                    result["dividend_cagr_3yr"] = round(cagr_3yr, 1)
            if len(years_sorted) >= 6:
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


# ─────────────────────────────────────────────────────────────────────────
# FORWARD-GROWTH SCORERS (Commit B — building blocks, no callers yet)
# ─────────────────────────────────────────────────────────────────────────
# Five components that triangulate 10-15y growth potential, designed to be
# combined into a single forward_growth_score (Commit C). Each returns
# 0-100. Missing data defaults to neutral 50 (per dividend-tier convention).

def _rd_threshold_for_sector(sector: str) -> float:
    """
    Return the R&D-intensity threshold (% of revenue) that represents
    "exceptional" investment for the given sector. Used by
    _score_innovation_investment for sector-calibrated scoring.

    Returns -1 for sectors where R&D is not meaningful (banks, insurance);
    caller should treat -1 as "skip this component, return neutral 50".

    Mapping is against the 23 distinct sector strings observed in
    portfolio_watchlist as of 2026-05-08 (via diagnostic). Includes both
    Bloomberg-style labels ("Consumer, Cyclical") and hand-coded labels
    from CANDIDATE_POOLS / breakthrough returns ("Cloud Data", "Super App").
    Falls through to substring matching for unmapped values.
    """
    if not sector:
        return 8.0

    # ── Exact-match the 23 known sectors first ──
    EXACT = {
        # Software-tier (18%)
        "Cloud Data": 18.0,
        "Cybersecurity": 18.0,
        "E-commerce/Fintech": 18.0,
        "E-commerce/Gaming": 18.0,
        "Enterprise Software": 18.0,
        "Super App": 18.0,
        # Biotech-tier (25%)
        "Genomics": 25.0,
        # Semiconductor-tier (12%)
        "Semiconductors": 12.0,
        "Semiconductor Equipment": 12.0,
        "EV/Energy/AI": 12.0,
        "Technology": 12.0,  # generic, between hardware & software
        # Servers/Infrastructure — adjacent to semis (10%)
        "Servers/Infrastructure": 10.0,
        # Healthcare equipment (8%)
        "Healthcare": 8.0,
        # Industrials (6%)
        "Industrial": 6.0,
        # Telecom/Media (4%)
        "Communications": 4.0,
        # Consumer (3%)
        "Consumer, Cyclical": 3.0,
        "Consumer, Non-cyclical": 3.0,
        # Energy/Materials/Utilities (2%)
        "Energy": 2.0,
        "Basic Materials": 2.0,
        "Nuclear/Clean Energy": 2.0,
        "Utilities": 2.0,
        # Financial — skip
        "Financial": -1.0,
    }
    if sector in EXACT:
        return EXACT[sector]

    # ── Fallback: loose substring match for unknown labels ──
    s = sector.lower()
    if any(k in s for k in ("bank", "insurance", "financial")):
        return -1.0
    if any(k in s for k in ("biotech", "pharma", "drug", "life science")):
        return 25.0
    if any(k in s for k in ("software", "internet", "saas")):
        return 18.0
    if any(k in s for k in ("semiconductor", "chip")):
        return 12.0
    if any(k in s for k in ("medical device", "health")):
        return 8.0
    if any(k in s for k in ("aerospace", "defense", "industrial", "machinery", "automotive")):
        return 6.0
    if any(k in s for k in ("consumer", "retail", "food", "beverage", "apparel")):
        return 3.0
    if any(k in s for k in ("energy", "oil", "gas", "mining", "material", "chemical", "utilit", "metal")):
        return 2.0
    if any(k in s for k in ("telecom", "media", "communication")):
        return 4.0
    if "tech" in s:
        return 12.0  # generic tech catch-all
    return 8.0


def _score_revenue_durability(fmp: dict) -> float:
    """
    Score 0-100 for revenue durability — the foundation of long-term
    compounding. Three components:
      - 5yr revenue CAGR raw level (60 pts)
      - YoY consistency vs 5yr average (25 pts)
      - Floor for no-growth structural fail (caps at 30 if revenue is flat/declining)
    """
    avg = fmp.get("revenue_avg_pct")
    yoy = fmp.get("revenue_yoy_pct")
    if avg is None or yoy is None:
        return 50.0  # neutral when data is missing

    # ── Component A: 5yr CAGR raw (60 pts) ──
    if avg < 0:
        cagr_score = 0
    elif avg < 5:
        cagr_score = 15
    elif avg < 10:
        cagr_score = 30
    elif avg < 15:
        cagr_score = 45
    elif avg < 20:
        cagr_score = 55
    elif avg < 30:
        cagr_score = 60   # sweet spot
    else:
        cagr_score = 50   # >30% might be unsustainable acceleration

    # ── Component B: YoY consistency vs avg (25 pts) ──
    if avg == 0:
        consistency_score = 12  # neutral, can't compute ratio
    else:
        # Distance from YoY to 5yr avg, normalized
        deviation_pct = abs(yoy - avg) / max(abs(avg), 1.0) * 100
        if deviation_pct <= 20:
            consistency_score = 25
        elif deviation_pct <= 50:
            consistency_score = 25 - (deviation_pct - 20) * (20.0 / 30.0)
        else:
            consistency_score = 5

    # ── Component C: 15 pts for positive growth signal (catches simple "growing" baseline) ──
    if avg > 0 and yoy > 0:
        positive_score = 15
    elif avg > 0 or yoy > 0:
        positive_score = 8
    else:
        positive_score = 0

    total = cagr_score + consistency_score + positive_score

    # Structural floor: if avg revenue growth is non-positive, cap at 30
    if avg <= 0:
        total = min(total, 30)

    return round(min(100, max(0, total)), 1)


def _score_compounding_quality(fmp: dict) -> float:
    """
    Score 0-100 for compounding quality (Buffett's compound machine).
    Uses ROIC sustained over 5 years.
      - 5yr ROIC avg (70 pts)
      - 5yr ROIC min as floor (30 pts) — catches bad-year disasters
    """
    avg = fmp.get("roic_5yr_avg")
    if avg is None:
        return 50.0  # neutral when ROIC unavailable

    # ── Avg ROIC level (70 pts) ──
    if avg < 5:
        avg_score = 0
    elif avg < 10:
        avg_score = 20   # mediocre — barely covers cost of capital
    elif avg < 15:
        avg_score = 40   # decent
    elif avg < 20:
        avg_score = 55   # strong
    elif avg < 25:
        avg_score = 65   # excellent compounder
    else:
        avg_score = 70   # elite — Costco/MSFT/MA tier

    # ── ROIC min floor (30 pts) ──
    roic_min = fmp.get("roic_5yr_min")
    if roic_min is None:
        min_score = 15  # neutral when min unavailable
    elif roic_min < 0:
        min_score = 0
    elif roic_min < 5:
        min_score = 10
    elif roic_min < 10:
        min_score = 20
    else:
        min_score = 30  # never broke down

    return round(min(100, avg_score + min_score), 1)


def _score_operating_leverage(fmp: dict) -> float:
    """
    Score 0-100 for operating leverage. Captures whether growth is
    profitable AND getting more profitable.
      - Operating margin level (50 pts)
      - Operating margin trend (30 pts)
      - Combined growth-x-profit signal (20 pts)
    """
    op_margin = fmp.get("operating_margin_pct")
    if op_margin is None:
        return 50.0

    # ── Operating margin level (50 pts) ──
    if op_margin < 0:
        level_score = 0
    elif op_margin < 10:
        level_score = 15
    elif op_margin < 20:
        level_score = 30
    elif op_margin < 30:
        level_score = 40
    else:
        level_score = 50  # 30%+ — software/quality territory

    # ── Operating margin trend (30 pts) ──
    trend = fmp.get("operating_margin_trend")
    if trend is None:
        trend_score = 15  # neutral
    elif trend < -3:
        trend_score = 0
    elif trend < 0:
        trend_score = 5
    elif trend < 2:
        trend_score = 15
    elif trend < 5:
        trend_score = 25
    else:
        trend_score = 30

    # ── Combined growth × profit (20 pts) ──
    yoy = fmp.get("revenue_yoy_pct")
    if yoy is None or op_margin <= 0:
        combined_score = 5  # can't compute meaningfully
    else:
        # raw: yoy% * op_margin / 100, e.g. 25% growth at 30% margin = 7.5
        raw = yoy * op_margin / 100
        if raw < 0:
            combined_score = 0
        elif raw < 2:
            combined_score = 5
        elif raw < 5:
            combined_score = 10
        elif raw < 10:
            combined_score = 15
        else:
            combined_score = 20

    return round(min(100, level_score + trend_score + combined_score), 1)


def _score_innovation_investment(fmp: dict, sector: str = "") -> float:
    """
    Score 0-100 for R&D investment. Sector-calibrated.

    For financial-type sectors (banks, insurance), R&D is not meaningful
    — returns neutral 50.

    For other sectors:
      - R&D intensity vs sector "exceptional" threshold (60 pts)
      - R&D growth trend (40 pts)
    """
    threshold = _rd_threshold_for_sector(sector)
    if threshold < 0:
        return 50.0  # financial-type sector, R&D not meaningful

    rd_intensity = fmp.get("rd_intensity_pct")
    if rd_intensity is None:
        return 50.0  # data missing

    # ── R&D intensity vs threshold (60 pts) ──
    ratio = rd_intensity / threshold if threshold > 0 else 0
    if ratio < 0.3:
        intensity_score = 0  # under-investing
    elif ratio < 0.7:
        intensity_score = 20
    elif ratio < 1.0:
        intensity_score = 40  # matching peers
    elif ratio < 1.5:
        intensity_score = 55
    else:
        intensity_score = 60  # elite R&D investment

    # ── R&D growth trend (40 pts) ──
    trend = fmp.get("rd_intensity_trend")
    if trend is None:
        trend_score = 20  # neutral
    elif trend < -2:
        trend_score = 0  # cutting R&D — harvesting mode
    elif trend < 0:
        trend_score = 10
    elif trend < 1:
        trend_score = 20  # stable
    elif trend < 3:
        trend_score = 30  # investing more
    else:
        trend_score = 40  # aggressive ramp

    return round(min(100, intensity_score + trend_score), 1)


def _score_capital_efficiency(fmp: dict) -> float:
    """
    Score 0-100 for capital efficiency. Captures whether the company
    destroys value through bad capital deployment.
      - FCF margin trend (35 pts)
      - Share dilution / buyback (35 pts)
      - Goodwill stability (30 pts) — proxy for value-destroying M&A
    """
    # ── FCF margin trend (35 pts) ──
    fcf_trend = fmp.get("fcf_margin_trend")
    if fcf_trend is None:
        fcf_score = 17  # neutral
    elif fcf_trend < -3:
        fcf_score = 0
    elif fcf_trend < 0:
        fcf_score = 10
    elif fcf_trend < 2:
        fcf_score = 20  # stable
    elif fcf_trend < 5:
        fcf_score = 30  # improving
    else:
        fcf_score = 35  # strong improvement

    # ── Share dilution / buyback (35 pts) ──
    dilution = fmp.get("share_dilution_pct_3yr")
    if dilution is None:
        dilution_score = 17  # neutral
    elif dilution > 10:
        dilution_score = 0   # heavy dilution
    elif dilution > 5:
        dilution_score = 10
    elif dilution > 0:
        dilution_score = 20  # modest dilution
    elif dilution > -5:
        dilution_score = 30  # modest buybacks
    else:
        dilution_score = 35  # significant buybacks

    # ── Goodwill stability (30 pts) ──
    gw_change = fmp.get("goodwill_to_assets_change_pct")
    if gw_change is None:
        gw_score = 20  # neutral when data missing
    elif gw_change > 15:
        gw_score = 0   # huge M&A bloat
    elif gw_change > 5:
        gw_score = 10
    elif gw_change > 0:
        gw_score = 20  # some M&A, normal
    elif gw_change > -5:
        gw_score = 30  # organic growth, no destructive M&A
    else:
        gw_score = 30  # impairments — neutral

    return round(min(100, fcf_score + dilution_score + gw_score), 1)


def _score_forward_growth(fmp: dict, sector: str = "") -> float:
    """
    Combine the five forward-growth sub-scorers into a single 0-100
    composite. The screener-side companion to _score_dividend_total_return.

    Weights:
      Revenue durability    25%  — foundation of compounding
      Compounding quality   25%  — ROIC sustained over 5yr (Buffett metric)
      Operating leverage    20%  — profitable AND increasingly so
      Innovation investment 15%  — R&D vs sector peers + trend
      Capital efficiency    15%  — FCF trend, dilution, no destructive M&A

    Hard cap: 30 if structural quality has failed (3+ years of negative
    net income AND 3+ years of negative FCF over the 5-year window).
    """
    rev_dur = _score_revenue_durability(fmp)
    cmp_q = _score_compounding_quality(fmp)
    op_lev = _score_operating_leverage(fmp)
    rd = _score_innovation_investment(fmp, sector)
    cap_eff = _score_capital_efficiency(fmp)

    composite = (
        rev_dur * 0.25
        + cmp_q * 0.25
        + op_lev * 0.20
        + rd * 0.15
        + cap_eff * 0.15
    )

    # ── Structural-quality hard cap ──
    ni_neg = fmp.get("net_income_negative_years_5yr") or 0
    fcf_neg = fmp.get("fcf_negative_years_5yr") or 0
    if ni_neg >= 3 and fcf_neg >= 3:
        composite = min(composite, 30.0)

    return round(min(100, max(0, composite)), 1)


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
    forward_growth_score: float = 0
    # Sub-scores from the 5-component composite (Commit O — preserved for augmentation prompt)
    sub_revenue_durability: float = 0
    sub_compounding_quality: float = 0
    sub_operating_leverage: float = 0
    sub_innovation_investment: float = 0
    sub_capital_efficiency: float = 0
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
        # Build merged universe (CANDIDATE_POOLS + discovered_pool - evicted)
        growth_universe = _get_growth_universe()

        if regions is None:
            regions = list(growth_universe.keys())

        all_scores: list[StockScore] = []

        print(f"\n{'='*60}")
        print(f"PHASE 1: Screening regular universe ({len(regions)} regions)")
        print(f"{'='*60}")

        for region in regions:
            pool = growth_universe.get(region)
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

        dividend_universe = _get_dividend_universe()
        for region, pool in dividend_universe.items():
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
                    # Step B: persist to breakthrough_history pool (best-effort)
                    try:
                        from datetime import datetime as _dt_bt
                        _persist_breakthrough_appearance(
                            symbol=symbol,
                            exchange=candidate.get("exchange", "SMART"),
                            currency=candidate.get("currency", "USD"),
                            name=score.name,
                            megatrend=score.megatrend,
                            thesis=candidate.get("thesis", "") or candidate.get("rationale", ""),
                            run_date=_dt_bt.now(),
                        )
                    except Exception as _e:
                        print(f"  ⚠ breakthrough_history hook failed for {symbol}: {_e}")
                    status = "✅" if score.options_available else "⛔"
                    print(f"  {status} {score.symbol:8s} | MCap: ${score.market_cap/1e9:5.1f}B | {score.megatrend}")
                else:
                    print(f"  ⛔ {symbol:8s} | No IBKR data")
            except Exception as e:
                print(f"  ❌ {symbol:8s} | Error: {e}")
            time.sleep(0.3)

        # Step B: trim breakthrough_history pool to cap, protecting recent names.
        # Fires once per screener run, after all breakthrough names have been
        # persisted. Best-effort: failure logged, scan continues.
        try:
            _evict_breakthrough_overflow()
        except Exception as _e:
            print(f"  ⚠ breakthrough_history evict hook failed: {_e}")

        # ──────────────────────────────────────────────────────────────────
        # PHASE 2.5: Augmentation (Claude-driven swap proposals)
        # ──────────────────────────────────────────────────────────────────
        # Default OFF — manually flip AUGMENTATION_ENABLED at module top to enable.
        # When enabled, asks Claude for high-conviction names that would beat the
        # current rank-60 (growth) / rank-15 (dividend) cutoffs. Each proposal is
        # scored, audit-logged, and accepted if score > cutoff (margin = 0).
        if AUGMENTATION_ENABLED:
            from datetime import datetime as _dt
            # Lazy import — matches codebase pattern
            from src.core.database import get_session_factory

            print(f"\n{'='*60}")
            print(f"PHASE 2.5: Augmentation (Claude swap proposals)")
            print(f"{'='*60}")

            run_date = _dt.utcnow()
            pending_yaml_additions = {"growth": [], "dividend": []}
            non_breakthrough = [s for s in all_scores if s.tier != "breakthrough"]

            # Split same way PHASE 3 does (yield routing)
            _div_universe_aug = _get_dividend_universe()
            _div_pool_syms_aug = {str(sym) for pool in _div_universe_aug.values() for sym in pool["symbols"]}
            growth_pool = [s for s in non_breakthrough
                          if s.symbol not in _div_pool_syms_aug and s.dividend_yield <= 2.5]
            dividend_pool = [s for s in non_breakthrough
                            if s.symbol in _div_pool_syms_aug or s.dividend_yield > 2.5]

            # Sort each pool by its tier-relevant metric
            growth_pool.sort(key=lambda s: s.forward_growth_score or 0.0, reverse=True)
            dividend_pool.sort(key=lambda s: s.dividend_total_return_score or 0.0, reverse=True)

            # Build exclusion set: every symbol already considered (across all tiers + pools)
            _growth_universe_aug = _get_growth_universe()
            _growth_universe_syms = {str(sym) for pool in _growth_universe_aug.values() for sym in pool["symbols"]}
            exclusion = _growth_universe_syms | _div_pool_syms_aug | {s.symbol for s in all_scores}

            # Open audit session
            audit_session = get_session_factory()()
            try:
                # ── Growth tier augmentation ──
                if len(growth_pool) >= 60:
                    top_60 = growth_pool[:60]
                    ranks_61_120 = growth_pool[60:120]
                    cutoff_growth = top_60[-1].forward_growth_score or 0.0
                    print(f"\n  Growth: cutoff={cutoff_growth:.1f} (rank-60 of {len(growth_pool)} candidates)")

                    proposals = _get_growth_swaps(top_60, ranks_61_120, cutoff_growth, exclusion)
                    accepted_count = 0
                    for prop in proposals:
                        if _process_augmentation_proposal(prop, "growth", cutoff_growth, all_scores, audit_session, run_date, pending_yaml_additions, screener=self):
                            accepted_count += 1
                            exclusion.add(prop.get("symbol", ""))  # don't propose same symbol for dividend tier
                    print(f"  Growth: {accepted_count}/{len(proposals)} proposals accepted")
                else:
                    print(f"\n  Growth: skipped (only {len(growth_pool)} candidates, need 60+)")

                # ── Dividend tier augmentation ──
                if len(dividend_pool) >= 15:
                    top_15 = dividend_pool[:15]
                    ranks_16_45 = dividend_pool[15:45]
                    cutoff_div = top_15[-1].dividend_total_return_score or 0.0
                    print(f"\n  Dividend: cutoff={cutoff_div:.1f} (rank-15 of {len(dividend_pool)} candidates)")

                    proposals = _get_dividend_swaps(top_15, ranks_16_45, cutoff_div, exclusion)
                    accepted_count = 0
                    for prop in proposals:
                        if _process_augmentation_proposal(prop, "dividend", cutoff_div, all_scores, audit_session, run_date, pending_yaml_additions, screener=self):
                            accepted_count += 1
                    print(f"  Dividend: {accepted_count}/{len(proposals)} proposals accepted")
                else:
                    print(f"\n  Dividend: skipped (only {len(dividend_pool)} candidates, need 15+)")

                audit_session.commit()
                print(f"\n  Audit log committed.")
                # Persist accepted symbols to discovered_pool.yaml so future runs see them
                _persist_augmentation_acceptances(pending_yaml_additions)
                # Evict overflow if pool grew beyond caps
                _evict_overflow_from_discovered_pool()
            except Exception as e:
                audit_session.rollback()
                print(f"\n  ❌ Augmentation failed: {type(e).__name__}: {e}")
                # Don't re-raise — augmentation is best-effort, screener should still complete
            finally:
                audit_session.close()

        print(f"\n{'='*60}")
        print(f"PHASE 3: Building portfolio universe")
        print(f"{'='*60}")

        # Include both DIVIDEND_CANDIDATES symbols AND discovered_pool dividend names
        _dividend_universe = _get_dividend_universe()
        _dividend_pool_symbols = {str(sym) for pool in _dividend_universe.values() for sym in pool["symbols"]}
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
        score.forward_growth_score = _score_forward_growth(fmp, score.sector)
        # Preserve sub-scores for augmentation prompt visibility (Commit O)
        score.sub_revenue_durability = _score_revenue_durability(fmp)
        score.sub_compounding_quality = _score_compounding_quality(fmp)
        score.sub_operating_leverage = _score_operating_leverage(fmp)
        score.sub_innovation_investment = _score_innovation_investment(fmp, score.sector)
        score.sub_capital_efficiency = _score_capital_efficiency(fmp)

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

        # Commit E (May 10): portfolio_score = forward_growth_score directly.
        # forward_growth_score is the Buffett-style fair-price composite
        # (30% raw + 70% compound quality across 5 sub-scorers).
        # The old 40/25/35 blend is preserved in growth_score / valuation_score /
        # quality_score on the StockScore (still computed at lines 2454-2456)
        # for diagnostic visibility.
        score.portfolio_score = round(score.forward_growth_score or 0.0, 1)
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
