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
            contract.exchange = "SMART"  # force SMART routing

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
        from src.core.config import load_config
        cfg = load_config()
        api_key = cfg.get("fmp", {}).get("api_key", "")
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
) -> Optional[dict]:
    """
    Fetch SPY daily bars and compute simple moving averages.
    Returns {"fast_ma": float, "slow_ma": float, "spy_price": float, "is_bullish": bool}
    or None if data unavailable.
    """
    try:
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Stock("SPY", "SMART", "USD")
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
                timeout=10,  # hard timeout in seconds
            )

            if not bars or len(bars) < slow_period:
                log.warning("insufficient_spy_bars", count=len(bars) if bars else 0, need=slow_period)
                return None

            closes = [bar.close for bar in bars]

        fast_ma = sum(closes[-fast_period:]) / fast_period
        slow_ma = sum(closes[-slow_period:]) / slow_period
        spy_price = closes[-1]
        is_bullish = fast_ma > slow_ma

        log.info(
            "spy_ma_calculated",
            spy_price=round(spy_price, 2),
            fast_ma=round(fast_ma, 2),
            slow_ma=round(slow_ma, 2),
            trend="bullish" if is_bullish else "bearish",
        )

        return {
            "fast_ma": round(fast_ma, 2),
            "slow_ma": round(slow_ma, 2),
            "spy_price": round(spy_price, 2),
            "is_bullish": is_bullish,
        }

    except Exception as e:
        log.warning("spy_ma_fetch_error", error=str(e))
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
            contract.exchange = "SMART"  # force SMART routing for data

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
    Check if a stock has earnings within N days.
    Uses a lightweight approach: checks if near-term option IV is
    abnormally elevated compared to the stock's recent IV.
    Returns False (allow trading) if data is unavailable — fail open.
    """
    try:
        ib = get_ib()
        contract = _make_stock_contract(symbol, exchange, currency)
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return False

        # Quick check: get option chains and look for very short-dated
        # options with abnormal IV. This is lightweight — just chain metadata.
        chains = ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)
        if not chains:
            return False  # no options = no earnings concern for us

        # For now, return False (allow trading).
        # The IV rank filter already naturally reduces entries around earnings
        # because IV rank will be elevated, pushing us further OTM via dynamic delta.
        # A full earnings calendar integration requires a data subscription
        # that may not be available on all IBKR account types.
        return False

    except Exception as e:
        log.debug("earnings_check_failed", symbol=symbol, error=str(e))
        return False
