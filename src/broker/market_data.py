"""
Market data — stock quotes, option chains, VIX, SPY MA.
All contract creation uses per-stock exchange and currency from the watchlist.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ib_insync import Stock, Index, Option, Contract, OptionChain

from src.broker.connection import get_ib, get_ib_lock
from src.core.config import get_settings
from src.core.logger import get_logger

log = get_logger(__name__)


# ── Per-symbol fail cache ────────────────────────────────────
# When a symbol fails price/IV fetch repeatedly, skip it temporarily
# to avoid IBKR pacing violations (60 historical data requests / 10 min)
_price_fail_counts: dict[str, int] = {}
_price_fail_until: dict[str, datetime] = {}
_PRICE_FAIL_THRESHOLD = 5       # failures before temporary skip
_PRICE_FAIL_COOLDOWN_MIN = 5   # minutes to skip after threshold reached


# Exchanges where IBKR's SMART router can re-route after qualifyContracts.
# Direct-only markets (MEXI, JSE, TASE, BVMF, VSE) must keep their original exchange
# after qualification — overriding to SMART breaks historical-data requests with Error 200.
_SMART_ROUTABLE_EXCHANGES = {
    "SMART", "NYSE", "NASDAQ", "ARCA", "BATS", "AMEX", "ISLAND",
    "LSE", "IBIS", "SBF", "AEB", "SWX", "BM", "BVME", "SFB",
    "CSE", "HEX", "OSE", "ENEXT.BE", "ISE",
    "TSE", "TSEJ", "SEHK", "SGX", "ASX", "KSE", "NSE", "IDX", "TWSE",
}


def _is_symbol_blocked(symbol: str) -> bool:
    """Check if a symbol is temporarily blocked due to repeated failures."""
    until = _price_fail_until.get(symbol)
    if until and datetime.now() < until:
        return True
    if until and datetime.now() >= until:
        # Cooldown expired, reset
        _price_fail_counts.pop(symbol, None)
        _price_fail_until.pop(symbol, None)
    return False


def _record_symbol_failure(symbol: str):
    """Record a failure for a symbol; block it after threshold."""
    count = _price_fail_counts.get(symbol, 0) + 1
    _price_fail_counts[symbol] = count
    if count >= _PRICE_FAIL_THRESHOLD:
        blocked_until = datetime.now() + timedelta(minutes=_PRICE_FAIL_COOLDOWN_MIN)
        _price_fail_until[symbol] = blocked_until
        log.info("price_symbol_blocked", symbol=symbol, failures=count,
                 blocked_until=blocked_until.strftime("%H:%M:%S"))


def _record_symbol_success(symbol: str):
    """Reset failure counter on success."""
    _price_fail_counts.pop(symbol, None)
    _price_fail_until.pop(symbol, None)


# ── Helpers ─────────────────────────────────────────────────
def _ensure_market_data_type():
    """Set market data type.
    
    Type 4 = delayed frozen — works with reqHistoricalData without
    needing real-time streaming subscriptions. Falls back gracefully.
    """
    try:
        ib = get_ib()
        ib.reqMarketDataType(4)  # 4 = delayed frozen (most permissive)
    except Exception:
        pass


def _make_stock_contract(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    primary_exchange: str | None = None,
) -> Stock:
    """Create a Stock contract with correct exchange/currency."""
    contract = Stock(symbol, exchange, currency)
    if primary_exchange:
        contract.primaryExchange = primary_exchange
    return contract


# ── Price data ──────────────────────────────────────────────
def get_stock_price(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[float]:
    """Get the last price for a stock using historical data (more reliable than streaming)."""
    if _is_symbol_blocked(symbol):
        log.debug("price_symbol_skipped_blocked", symbol=symbol)
        return None
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = _make_stock_contract(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            if exchange in _SMART_ROUTABLE_EXCHANGES:
                contract.exchange = "SMART"  # force SMART routing where supported

            # Try TRADES first, then MIDPOINT (needed for many European stocks)
            for what in ("TRADES", "MIDPOINT"):
                try:
                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime="",
                        durationStr="2 D",
                        barSizeSetting="1 day",
                        whatToShow=what,
                        useRTH=False,
                        formatDate=1,
                        timeout=8,
                    )
                    if bars:
                        _record_symbol_success(symbol)
                        try:
                            from src.scheduler.jobs import record_price_success
                            record_price_success()
                        except Exception:
                            pass
                        return float(bars[-1].close)
                except Exception as e:
                    log.debug("price_request_failed", symbol=symbol, what=what, error=str(e) or repr(e))
                ib.sleep(0.5)  # brief pause before retry

        log.warning("no_price_data", symbol=symbol, exchange=exchange)
        _record_symbol_failure(symbol)
        try:
            from src.scheduler.jobs import record_price_failure
            record_price_failure()
        except Exception:
            pass
        return None
    except Exception as e:
        log.warning("price_fetch_error", symbol=symbol, error=str(e))
        _record_symbol_failure(symbol)
        try:
            from src.scheduler.jobs import record_price_failure
            record_price_failure()
        except Exception:
            pass
        return None


def _get_vix_from_fmp() -> Optional[float]:
    """Fallback: get VIX from FMP API."""
    try:
        import requests
        from src.portfolio.fmp import get_fmp_key
        api_key = get_fmp_key()
        if not api_key:
            return None
        url = f"https://financialmodelingprep.com/stable/quote?symbol=%5EVIX&apikey={api_key}"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data and isinstance(data, list) and "price" in data[0]:
            vix = float(data[0]["price"])
            log.info("vix_fetched_fmp", vix=vix)
            return vix
    except Exception as e:
        log.warning("vix_fmp_error", error=str(e))
    return None


def get_52week_high(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Optional[float]:
    """
    Get 52-week high price for a symbol using IBKR historical data.
    Requests 252 trading days of daily bars and returns the max high.
    """
    try:
        from ib_insync import Stock
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Stock(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="52 W",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                timeout=10,
            )
            if not bars:
                log.warning("no_52week_bars", symbol=symbol)
                return None
            high = max(bar.high for bar in bars)
            log.info("52week_high_fetched", symbol=symbol, high=round(high, 2))
            return high
    except Exception as e:
        log.warning("52week_high_error", symbol=symbol, error=str(e))
        return None


def get_vix() -> Optional[float]:
    """Get the current VIX level. Tries IBKR first, falls back to FMP."""
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Index("VIX", "CBOE", "USD")
            ib.qualifyContracts(contract)

            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=8,
            )
            if bars:
                vix = float(bars[-1].close)
                log.info("vix_fetched", vix=vix)
                return vix

        log.warning("no_vix_data_ibkr_trying_fmp")
    except Exception as e:
        log.warning("vix_ibkr_error", error=str(e))

    # Fallback to FMP
    return _get_vix_from_fmp()


# ── Option chains ───────────────────────────────────────────
def get_option_chains(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> list[OptionChain]:
    """Get available option chains for a symbol on any exchange."""
    ib = get_ib()
    # Always qualify the stock contract via SMART routing
    contract = _make_stock_contract(symbol, "SMART", currency)
    ib.qualifyContracts(contract)
    chains = ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)
    log.info("option_chains_raw", symbol=symbol, exchange=exchange,
             count=len(chains),
             exchanges=[c.exchange for c in chains] if chains else [])
    return chains


def _find_best_option_exchange(chains: list[OptionChain], preferred: str = "SMART") -> OptionChain | None:
    """Pick the best option chain exchange — prefer SMART, then the stock's own exchange."""
    if not chains:
        return None
    for c in chains:
        if c.exchange == preferred:
            return c
    # Fallback: first available
    return chains[0]


def get_put_contracts(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    max_dte: int = 2,
    min_dte: int = 0,
) -> list[Option]:
    """
    Find put option contracts within the DTE range.
    Uses the stock's exchange/currency for contract creation.
    """
    ib = get_ib()
    chains = get_option_chains(symbol, exchange, currency)

    if not chains:
        log.info("no_option_chains", symbol=symbol, exchange=exchange)
        return []

    chain = _find_best_option_exchange(chains, preferred="SMART" if exchange == "SMART" else exchange)
    if chain is None:
        log.info("no_matching_option_exchange", symbol=symbol, preferred=exchange,
                 available=[c.exchange for c in chains])
        return []

    today = datetime.now().date()
    target_expiries = []
    for exp_str in sorted(chain.expirations):
        exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
        dte = (exp_date - today).days
        if min_dte <= dte <= max_dte:
            target_expiries.append(exp_str)

    if not target_expiries:
        log.info("no_expiries_in_range", symbol=symbol, dte_range=(min_dte, max_dte),
                 available=[s for s in sorted(chain.expirations)[:5]])
        return []

    price = get_stock_price(symbol, "SMART", currency)
    if not price:
        return []

    # OTM puts = strikes below current price, within reasonable range
    opt_exchange = chain.exchange
    contracts = []
    
    log.info("option_chain_data", symbol=symbol, exchange=opt_exchange,
             expirations=len(target_expiries), strikes=len(chain.strikes),
             price=round(price, 2), expiry_list=target_expiries[:5])
    
    for exp in target_expiries:
        for strike in chain.strikes:
            if strike < price and strike > price * 0.85:
                opt = Option(symbol, exp, strike, "P", opt_exchange, currency=currency)
                # Set tradingClass for non-SMART exchanges to resolve ambiguity
                # (e.g. EUREX has NESE vs NESN classes for the same underlying)
                if opt_exchange != "SMART":
                    opt.tradingClass = chain.tradingClass
                contracts.append(opt)

    if not contracts:
        return []

    qualified = ib.qualifyContracts(*contracts)
    return [c for c in qualified if c.conId > 0]


def get_option_greeks(contracts: list[Option]) -> dict:
    """
    Fetch market data including greeks for a list of option contracts.
    Returns {conId: ticker} dict.
    """
    # Serialize all IB calls through get_ib_lock() like every other fetcher here.
    # Unlocked, this raced the shared event loop against concurrent reqMktData and
    # produced empty tickers ("no live ask").
    with get_ib_lock():
        _ensure_market_data_type()
        ib = get_ib()
        tickers = {}
        for contract in contracts:
            ticker = ib.reqMktData(contract, "100", False, False)
            tickers[contract.conId] = ticker

        ib.sleep(3)

        for contract in contracts:
            ib.cancelMktData(contract)

        return tickers


def get_call_contracts(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    min_dte: int = 5,
    max_dte: int = 14,
    min_strike: Optional[float] = None,
) -> list[Option]:
    """
    Find call option contracts for covered call writing.
    If min_strike is set, only returns strikes above it (cost basis).
    """
    ib = get_ib()
    chains = get_option_chains(symbol, exchange, currency)

    if not chains:
        return []

    chain = _find_best_option_exchange(chains, preferred="SMART" if exchange == "SMART" else exchange)
    if chain is None:
        return []

    today = datetime.now().date()
    target_expiries = []
    for exp_str in sorted(chain.expirations):
        exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
        dte = (exp_date - today).days
        if min_dte <= dte <= max_dte:
            target_expiries.append(exp_str)

    if not target_expiries:
        return []

    price = get_stock_price(symbol, "SMART", currency)
    if not price:
        return []

    lower_bound = min_strike if min_strike else price
    opt_exchange = chain.exchange
    contracts = []
    for exp in target_expiries:
        for strike in chain.strikes:
            if strike > lower_bound and strike < price * 1.15:
                opt = Option(symbol, exp, strike, "C", opt_exchange, currency=currency)
                if opt_exchange != "SMART":
                    opt.tradingClass = chain.tradingClass
                contracts.append(opt)

    if not contracts:
        return []

    qualified = ib.qualifyContracts(*contracts)
    return [c for c in qualified if c.conId > 0]


# ── SPY Moving Average Gate ────────────────────────────────
def get_spy_moving_averages(
    fast_period: int = 10,
    slow_period: int = 20,
    trend_period: int = 50,
    long_trend_period: int = 200,
) -> Optional[dict]:
    """
    Fetch SPY daily bars and compute simple moving averages.
    Returns dict with fast_ma, slow_ma, ma50 (trend filter), ma200 (long-trend),
    spy_price, is_bullish (fast>slow), ma50 + ma200 (may be None if insufficient
    history), distance_below_ma50 and distance_below_ma200 (positive = below).
    Returns None if data fully unavailable.
    """
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Stock("SPY", "SMART", "USD")
            ib.qualifyContracts(contract)

            # Need enough bars for the longest MA (MA200 by default)
            longest = max(slow_period, trend_period, long_trend_period)
            duration = f"{longest + 5} D"
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=15,  # hard timeout in seconds (longer for 50D)
            )

            if not bars or len(bars) < slow_period:
                log.warning("insufficient_spy_bars", count=len(bars) if bars else 0, need=slow_period)
                return None

            closes = [bar.close for bar in bars]

        fast_ma = sum(closes[-fast_period:]) / fast_period
        slow_ma = sum(closes[-slow_period:]) / slow_period
        spy_price = closes[-1]
        is_bullish = fast_ma > slow_ma

        # MA50 if we have enough bars
        if len(closes) >= trend_period:
            ma50 = sum(closes[-trend_period:]) / trend_period
            distance_below_ma50 = (ma50 - spy_price) / ma50  # + when below, - when above
        else:
            ma50 = None
            distance_below_ma50 = None
            log.warning("insufficient_bars_for_ma50", have=len(closes), need=trend_period)

        # MA200 (long-trend / bear-market gate)
        if len(closes) >= long_trend_period:
            ma200 = sum(closes[-long_trend_period:]) / long_trend_period
            distance_below_ma200 = (ma200 - spy_price) / ma200
        else:
            ma200 = None
            distance_below_ma200 = None
            log.warning("insufficient_bars_for_ma200", have=len(closes), need=long_trend_period)

        log.info(
            "spy_ma_calculated",
            spy_price=round(spy_price, 2),
            fast_ma=round(fast_ma, 2),
            slow_ma=round(slow_ma, 2),
            ma50=round(ma50, 2) if ma50 is not None else None,
            ma200=round(ma200, 2) if ma200 is not None else None,
            distance_below_ma50=round(distance_below_ma50, 4) if distance_below_ma50 is not None else None,
            distance_below_ma200=round(distance_below_ma200, 4) if distance_below_ma200 is not None else None,
            trend="bullish" if is_bullish else "bearish",
        )

        return {
            "fast_ma": round(fast_ma, 2),
            "slow_ma": round(slow_ma, 2),
            "ma50": round(ma50, 2) if ma50 is not None else None,
            "ma200": round(ma200, 2) if ma200 is not None else None,
            "distance_below_ma50": round(distance_below_ma50, 4) if distance_below_ma50 is not None else None,
            "distance_below_ma200": round(distance_below_ma200, 4) if distance_below_ma200 is not None else None,
            "spy_price": round(spy_price, 2),
            "is_bullish": is_bullish,
        }

    except Exception as e:
        log.warning("spy_ma_fetch_error", error=str(e))
        return None


def compute_grind_signals(
    symbols: list[str],
    rv_window_days: int = 60,
    trend_window_days: int = 180,
) -> Optional[dict]:
    """Compute the two signals the cash-and-carry grind detector needs:

    1. Universe-median trailing N-day realized vol (annualized, decimal).
    2. SPY trailing M-day price return (percent).

    Fetches daily bars from IBKR for SPY + each symbol. Should be called once
    per business day (cache the result in SystemState; downstream scans read
    cached value). All fetches use the existing ib_lock + market_data_type
    plumbing so we don't conflict with other IBKR calls.

    Returns dict with keys: realized_vol_median (float | None), n_symbols_used
    (int), spy_trend_return_pct (float | None), n_spy_bars (int). Returns
    None only on catastrophic failure (no IBKR connection at all). Individual
    symbol failures degrade gracefully — the median is taken over whatever
    symbols return data.
    """
    import math as _math
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()

            # SPY: fetch trend_window_days + 5 bars for the rolling return.
            spy_contract = Stock("SPY", "SMART", "USD")
            ib.qualifyContracts(spy_contract)
            spy_bars = ib.reqHistoricalData(
                spy_contract,
                endDateTime="",
                durationStr=f"{trend_window_days + 5} D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=15,
            )
            spy_closes = [b.close for b in (spy_bars or [])]
            spy_trend_return_pct: Optional[float] = None
            if len(spy_closes) > trend_window_days:
                back = spy_closes[-trend_window_days - 1]
                cur = spy_closes[-1]
                if back > 0:
                    spy_trend_return_pct = (cur / back - 1.0) * 100.0
            n_spy_bars = len(spy_closes)

            # Per-symbol: fetch rv_window_days + 5 daily bars; compute realized
            # vol (std of log-returns × √252). Sequential — IBKR rate-limits
            # parallel reqHistoricalData. ~47 symbols × ~200ms = ~10s.
            rvs: list[float] = []
            for sym in symbols:
                try:
                    contract = Stock(sym, "SMART", "USD")
                    ib.qualifyContracts(contract)
                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime="",
                        durationStr=f"{rv_window_days + 5} D",
                        barSizeSetting="1 day",
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=1,
                        timeout=10,
                    )
                    closes = [b.close for b in (bars or []) if b.close and b.close > 0]
                    if len(closes) < rv_window_days:
                        continue
                    window = closes[-rv_window_days:]
                    rets = [
                        _math.log(window[i] / window[i - 1])
                        for i in range(1, len(window))
                        if window[i - 1] > 0
                    ]
                    if len(rets) < 5:
                        continue
                    mu = sum(rets) / len(rets)
                    var = sum((r - mu) ** 2 for r in rets) / max(len(rets) - 1, 1)
                    rvs.append(_math.sqrt(var) * _math.sqrt(252))
                except Exception as e:
                    log.debug("grind_symbol_fetch_failed", symbol=sym, error=str(e))
                    continue

        rv_median: Optional[float] = None
        if rvs:
            rvs.sort()
            mid = len(rvs) // 2
            rv_median = (
                rvs[mid] if len(rvs) % 2 == 1
                else (rvs[mid - 1] + rvs[mid]) / 2.0
            )

        log.info(
            "grind_signals_computed",
            rv_median=round(rv_median, 4) if rv_median is not None else None,
            n_symbols_used=len(rvs),
            spy_trend_pct=round(spy_trend_return_pct, 2) if spy_trend_return_pct is not None else None,
            n_spy_bars=n_spy_bars,
        )
        return {
            "realized_vol_median": rv_median,
            "n_symbols_used": len(rvs),
            "spy_trend_return_pct": spy_trend_return_pct,
            "n_spy_bars": n_spy_bars,
        }
    except Exception as e:
        log.warning("grind_signals_fetch_failed", error=str(e))
        return None


def get_stock_ma200(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    period: int = 200,
) -> Optional[dict]:
    """
    Fetch a single stock's daily bars and compute its trailing 200d SMA.
    Returns {"price": last_close, "ma200": float, "distance_below_ma200": float,
    "is_below": bool} or None if data unavailable / too short.

    Used by the per-name MA200 gate (risk.is_below_ma200) — skip writing puts
    on names trading in their own bear trend even when SPY is fine. Backtests
    (MarsWalk, all 11 regimes) show this beats SPY-MA200 by 2.7pp avg return
    AND -18pp on bear_2022.
    """
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Stock(symbol, exchange, currency)
            ib.qualifyContracts(contract)

            duration = f"{period + 10} D"
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=15,
            )
            if not bars or len(bars) < period:
                log.warning("insufficient_stock_bars", symbol=symbol,
                            count=len(bars) if bars else 0, need=period)
                return None

            closes = [b.close for b in bars]

        ma200 = sum(closes[-period:]) / period
        price = closes[-1]
        dist = (ma200 - price) / ma200 if ma200 else 0.0
        return {
            "price": round(price, 2),
            "ma200": round(ma200, 2),
            "distance_below_ma200": round(dist, 4),
            "is_below": price < ma200,
        }
    except Exception as e:
        log.warning("stock_ma200_fetch_error", symbol=symbol, error=str(e))
        return None


def get_regional_moving_averages(
    ticker: str,
    exchange: str = "SMART",
    currency: str = "USD",
    fast_period: int = 10,
    slow_period: int = 20,
) -> Optional[dict]:
    """
    Fetch daily bars for a regional ETF and compute simple moving averages.
    Used for EU (FEZ) and Asia (EWJ) regime detection.
    Returns {"fast_ma": float, "slow_ma": float, "price": float, "is_bullish": bool}
    or None if data unavailable.
    """
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Stock(ticker, exchange, currency)
            ib.qualifyContracts(contract)

            duration = f"{slow_period + 5} D"
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=10,
            )

            if not bars or len(bars) < slow_period:
                log.warning("insufficient_regional_bars", ticker=ticker, count=len(bars) if bars else 0, need=slow_period)
                return None

            closes = [bar.close for bar in bars]

        fast_ma = sum(closes[-fast_period:]) / fast_period
        slow_ma = sum(closes[-slow_period:]) / slow_period
        price = closes[-1]
        is_bullish = fast_ma > slow_ma

        log.info(
            "regional_ma_calculated",
            ticker=ticker,
            price=round(price, 2),
            fast_ma=round(fast_ma, 2),
            slow_ma=round(slow_ma, 2),
            trend="bullish" if is_bullish else "bearish",
        )

        return {
            "fast_ma": round(fast_ma, 2),
            "slow_ma": round(slow_ma, 2),
            "price": round(price, 2),
            "is_bullish": is_bullish,
        }

    except Exception as e:
        log.warning("regional_ma_fetch_error", ticker=ticker, error=str(e))
        return None


# ── IV Rank ─────────────────────────────────────────────────
def get_iv_rank(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    lookback_days: int = 252,
) -> Optional[float]:
    """
    Calculate IV rank (percentile) for a stock.
    Compares current IV to its range over the lookback period.
    Returns 0-100 (percentile) or None if unavailable.
    """
    if _is_symbol_blocked(symbol):
        log.debug("iv_rank_symbol_skipped_blocked", symbol=symbol)
        return None
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = _make_stock_contract(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            if exchange in _SMART_ROUTABLE_EXCHANGES:
                contract.exchange = "SMART"  # force SMART routing for data where supported

            # Get historical volatility data via option implied volatility
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=f"{lookback_days} D",
                barSizeSetting="1 day",
                whatToShow="OPTION_IMPLIED_VOLATILITY",
                useRTH=False,
                formatDate=1,
                timeout=10,
            )

            if not bars or len(bars) < 30:
                log.debug("insufficient_iv_data", symbol=symbol, bars=len(bars) if bars else 0)
                return None

            iv_values = [b.close for b in bars if b.close and b.close > 0]
            if len(iv_values) < 30:
                return None

        current_iv = iv_values[-1]
        min_iv = min(iv_values)
        max_iv = max(iv_values)

        if max_iv == min_iv:
            return 50.0  # flat IV, neutral rank

        iv_rank = ((current_iv - min_iv) / (max_iv - min_iv)) * 100

        log.debug(
            "iv_rank_calculated",
            symbol=symbol,
            current_iv=round(current_iv, 4),
            iv_rank=round(iv_rank, 1),
            min_iv=round(min_iv, 4),
            max_iv=round(max_iv, 4),
        )

        _record_symbol_success(symbol)
        return round(iv_rank, 1)

    except Exception as e:
        log.warning("iv_rank_fetch_error", symbol=symbol, error=str(e))
        _record_symbol_failure(symbol)
        return None


# ── Earnings Check ──────────────────────────────────────────
def has_upcoming_earnings(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    within_days: int = 3,
) -> bool:
    """
    Block puts on stocks with imminent earnings.

    FAIL-CLOSED: if we cannot determine the next earnings date (FMP refresh
    stale or failed, DB error), return True (block the trade). Earnings is
    the most predictable cause of overnight gap risk on a CSP — better to
    skip a trade than mis-trade through earnings.

    Data source: FMP /stable/earnings-calendar via
    src.portfolio.fmp.get_next_earnings_date(), which bulk-fetches the next
    7 days once per 24h and caches per-symbol in the `earnings_cache` table.

    Previously backed by IBKR CalendarReport via ibkr_fundamentals; that
    path is dead on these gateways (Error 10276 'News feed is not allowed'
    — verified 2026-05-19 with AAPL/MSFT/NVDA/GOOGL/META, all returned
    empty XML). Swap to FMP keeps the father's fail-CLOSED shell and the
    EarningsCache(status) schema; the bulk-refresh writes status='found'
    rows for symbols inside the 7d window and the absence of a row (when
    refresh is fresh) is positive evidence of 'no earnings in window'.

    `exchange`/`currency` kept for caller-signature compatibility with
    risk.check_earnings() and portfolio/buyer.py.
    """
    from datetime import date as _date
    try:
        from src.portfolio.fmp import get_next_earnings_date
        next_date, refresh_ok = get_next_earnings_date(symbol)
    except Exception as e:
        log.warning("earnings_check_failed", symbol=symbol, error=str(e))
        return True  # FAIL-CLOSED

    if not refresh_ok:
        log.info("earnings_check_unknown_blocked", symbol=symbol,
                  reason="fmp_refresh_stale_or_failed")
        return True  # FAIL-CLOSED

    if next_date is None:
        log.debug("earnings_check_no_upcoming", symbol=symbol)
        return False  # FMP refresh fresh + no row → no earnings in window

    days_until = (next_date - _date.today()).days
    blocked = 0 <= days_until <= within_days
    if blocked:
        log.info("earnings_check_blocked", symbol=symbol,
                  next_date=next_date.isoformat(), days_until=days_until,
                  within_days=within_days)
    else:
        log.debug("earnings_check_allowed", symbol=symbol,
                   next_date=next_date.isoformat(), days_until=days_until)
    return blocked



def get_stock_live_price(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[float]:
    """
    Fetch live last/bid/ask for a stock from IBKR using reqMktData.
    Returns last traded price, or None if unavailable.
    Used by CC profit checker for intraday price — get_stock_price returns yesterday close.
    """
    try:
        from src.broker.connection import get_ib_lock
        _ensure_market_data_type()
        with get_ib_lock():
            ib = get_ib()
            contract = Stock(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(3)
            ib.cancelMktData(contract)
            price = None
            if ticker.last and ticker.last > 0:
                price = ticker.last
            elif ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                price = (ticker.bid + ticker.ask) / 2
            elif ticker.close and ticker.close > 0:
                price = ticker.close
            log.debug("stock_live_price_fetched", symbol=symbol, price=price)
            return price
    except Exception as e:
        log.debug("stock_live_price_error", symbol=symbol, error=str(e))
        return None


def get_stock_live_quote(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[tuple[float, float, Optional[float]]]:
    """
    Fetch live bid/ask/last for a stock from IBKR using reqMktData.
    Returns (bid, ask, last) tuple, or None if no valid two-sided quote.

    Companion to get_stock_live_price() which returns a single price.
    This helper preserves bid/ask separately so callers can validate spreads
    (e.g., pre-market wheel-exit job rejecting wide-spread phantom quotes).

    Requires both bid > 0 AND ask > 0 to return a result. Single-sided quotes
    (e.g., illiquid pre-market with bid only) return None.
    """
    try:
        from src.broker.connection import get_ib_lock
        _ensure_market_data_type()
        with get_ib_lock():
            ib = get_ib()
            contract = Stock(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(3)
            ib.cancelMktData(contract)

            bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
            last = ticker.last if ticker.last and ticker.last > 0 else None

            if bid is None or ask is None:
                log.debug("stock_live_quote_no_two_sided", symbol=symbol,
                          bid=bid, ask=ask, last=last)
                return None

            log.debug("stock_live_quote_fetched", symbol=symbol,
                      bid=bid, ask=ask, last=last)
            return (bid, ask, last)
    except Exception as e:
        log.debug("stock_live_quote_error", symbol=symbol, error=str(e))
        return None


def get_option_live_price(
    symbol: str,
    expiry: str,
    strike: float,
    right: str = "P",
    exchange: str = "SMART",
    currency: str = "USD",
) -> tuple[Optional[float], Optional[float]]:
    """
    Fetch live bid/ask for a single option contract from IBKR.
    Returns (bid, ask) tuple, or (None, None) if unavailable.
    """
    try:
        # Serialize all IB calls through get_ib_lock() like every other fetcher here.
        # Unlocked, this raced the shared event loop against concurrent reqMktData and
        # returned an empty ask, which the CC profit-take check logged as
        # "cc_check_no_live_price" (7/cycle) and skipped.
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Option(symbol, expiry, strike, right, exchange, currency=currency)
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                log.debug("option_live_price_qualify_failed", symbol=symbol, strike=strike)
                return None, None

            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(3)
            ib.cancelMktData(contract)

            bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else None

            log.debug("option_live_price_fetched", symbol=symbol, strike=strike,
                      expiry=expiry, bid=bid, ask=ask)
            return bid, ask

    except Exception as e:
        log.debug("option_live_price_error", symbol=symbol, strike=strike, error=str(e))
        return None, None
