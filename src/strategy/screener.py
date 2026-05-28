"""
Option contract screener — selects the best put or call to trade.
Exchange and currency-aware for global stocks.

Uses Black-Scholes theoretical pricing computed from historical IV data.
No streaming market data subscriptions required — works on all markets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ib_insync import Option as IBOption

from src.broker.market_data import get_put_contracts, get_call_contracts, get_stock_price
from src.broker.greeks import compute_put_greeks, compute_call_greeks, get_current_iv
from src.strategy.option_scoring import score_put_candidates, score_call_candidates
from src.broker.connection import get_ib, get_ib_lock
from src.core.config import get_settings
from src.core.logger import get_logger

log = get_logger(__name__)


# ── Fee floor helpers ───────────────────────────────────────
_ESTIMATED_FEES = {
    "USD": 1.30, "AUD": 2.00, "GBP": 1.80, "EUR": 1.80,
    "CHF": 2.00, "JPY": 200.0, "NOK": 15.0, "DKK": 15.0, "CAD": 1.80,
}

def _passes_fee_floor(premium, contract_size, currency, contracts=1):
    cfg = get_settings().strategy
    # UK options prices are in pence — convert to pounds for fee comparison
    effective_premium = premium / 100.0 if currency == "GBP" else premium
    gross = effective_premium * contract_size * contracts
    minimum = _ESTIMATED_FEES.get(currency, 2.0) * contracts * cfg.min_net_premium_multiplier
    if gross < minimum:
        log.info("premium_below_fee_floor", premium=round(premium, 4),
                 gross=round(gross, 2), min_required=round(minimum, 2), currency=currency)
        return False
    return True

def _passes_min_price(stock_price, currency):
    floor = get_settings().strategy.min_stock_price.get(currency, 2.0)
    if stock_price < floor:
        log.info("stock_price_below_minimum", price=round(stock_price, 2), floor=floor, currency=currency)
        return False
    return True


@dataclass
class ScoredContract:
    contract: IBOption
    strike: float
    expiry: str
    delta: float
    bid: float
    ask: float
    mid: float
    iv: float
    open_interest: int
    score: float


def _weekend_theta_bonus(today, exp_date, dte: int) -> float:
    """#4 Weekend theta: fraction of the days-to-expiry that are non-trading
    (weekend) days. Rewards capturing decay over days with no market exposure
    (e.g. selling Friday into the weekend). Holidays not yet modelled."""
    if dte <= 0:
        return 0.0
    from datetime import timedelta
    non_trading = 0
    d = today
    for _ in range(dte):
        d = d + timedelta(days=1)
        if d.weekday() >= 5:  # Sat=5, Sun=6
            non_trading += 1
    return non_trading / dte


def screen_puts(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    delta_override: tuple[float, float] | None = None,
    stock_exchange: str | None = None,
    dte_min: int | None = None,
    dte_max: int | None = None,
) -> Optional[ScoredContract]:
    """
    Screen and rank put contracts for a symbol.
    delta_override: (min, max) to use instead of config defaults (for dynamic delta).
    Uses Black-Scholes theoretical pricing from historical IV data.
    Returns the best candidate or None.
    exchange: used for option chain queries (DTB, ICEEU, etc.)
    stock_exchange: used for stock price/IV lookups (defaults to exchange if not set)
    """
    cfg = get_settings().strategy
    stk_exchange = stock_exchange or exchange

    # Use passed DTE values if provided, otherwise fall back to config
    resolved_dte_min = dte_min if dte_min is not None else getattr(cfg, 'dte_min', 5)
    resolved_dte_max = dte_max if dte_max is not None else getattr(cfg, 'dte_max', 14)

    contracts = get_put_contracts(
        symbol,
        exchange=exchange,
        currency=currency,
        max_dte=resolved_dte_max,
        min_dte=resolved_dte_min,
    )

    if not contracts:
        log.info("no_put_contracts", symbol=symbol, exchange=exchange)
        return None

    # Get stock price and IV from historical data (no subscription needed)
    stock_price = get_stock_price(symbol, exchange=stk_exchange, currency=currency)
    if not stock_price or stock_price <= 0:
        log.info("no_stock_price_for_screening", symbol=symbol)
        return None

    if not _passes_min_price(stock_price, currency):
        return None

    ib = get_ib()
    iv = get_current_iv(ib, symbol, exchange=stk_exchange, currency=currency)
    if not iv or iv <= 0:
        log.info("no_iv_for_screening", symbol=symbol)
        return None

    log.info("bs_screening_puts", symbol=symbol, price=round(stock_price, 2),
             iv=round(iv, 3), contracts=len(contracts))

    today = datetime.now().date()
    delta_min = delta_override[0] if delta_override else cfg.delta_min
    delta_max = delta_override[1] if delta_override else cfg.delta_max
    candidates = score_put_candidates(
        stock_price, iv, contracts, cfg,
        delta_min, delta_max,
        resolved_dte_min, resolved_dte_max, today,
    )

    if not candidates:
        log.info("no_qualifying_puts", symbol=symbol,
                 total_contracts=len(contracts),
                 delta_range=(round(delta_min, 2), round(delta_max, 2)),
                 min_premium=cfg.min_premium, min_bid=cfg.min_bid)
        return None

    # Try top 2 candidates by score; return first that gets live quote AND passes fee floor.
    # BS is for selection only -- never return BS prices as order prices.
    sorted_candidates = sorted(candidates, key=lambda c: c.score, reverse=True)[:2]

    for idx, candidate in enumerate(sorted_candidates):
        try:
            ib = get_ib()
            with get_ib_lock():
                ticker = ib.reqMktData(candidate.contract, "", True, False)
                ib.sleep(2)
                ib.cancelMktData(candidate.contract)

            real_bid = ticker.bid
            real_ask = ticker.ask

            valid_bid = real_bid and real_bid > 0 and real_bid != float('inf') and real_bid != -1.0
            valid_ask = real_ask and real_ask > 0 and real_ask != float('inf') and real_ask != -1.0

            if not valid_bid:
                log.info("put_candidate_no_live_quote_trying_next",
                         symbol=symbol, strike=candidate.strike, rank=idx + 1)
                continue

            real_mid = round((real_bid + real_ask) / 2, 2) if valid_ask else real_bid

            best = ScoredContract(
                contract=candidate.contract,
                strike=candidate.strike,
                expiry=candidate.expiry,
                delta=candidate.delta,
                bid=round(real_bid, 2),
                ask=round(real_ask, 2) if valid_ask else candidate.ask,
                mid=real_mid,
                iv=candidate.iv,
                open_interest=candidate.open_interest,
                score=candidate.score,
            )

            if not _passes_fee_floor(best.bid, 100, currency, cfg.contracts_per_stock):
                log.info("put_candidate_fails_fee_floor_trying_next",
                         symbol=symbol, strike=best.strike, bid=best.bid, rank=idx + 1)
                continue

            log.info(
                "put_screened",
                symbol=symbol,
                exchange=exchange,
                strike=best.strike,
                expiry=best.expiry,
                delta=round(best.delta, 3),
                mid=round(best.mid, 2),
                iv=round(best.iv, 3),
                rank=idx + 1,
            )
            return best

        except Exception as e:
            log.warning("put_candidate_quote_exception",
                        symbol=symbol, strike=candidate.strike, error=str(e))
            continue

    log.info("put_candidates_exhausted_no_live",
             symbol=symbol, candidates_tried=len(sorted_candidates))
    return None


def screen_calls(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    min_strike: Optional[float] = None,
    delta_min_override: float | None = None,
    delta_max_override: float | None = None,
    stock_exchange: str | None = None,
    stock_price_override: float | None = None,
    max_dte_override: int | None = None,
    target_expiry: str | None = None,
) -> Optional[ScoredContract]:
    """
    Screen and rank call contracts for covered call writing.
    delta_min/max_override: for progressive strike management.
    stock_price_override: use live price instead of get_stock_price (yesterday close).
    max_dte_override: cap DTE for roll-up scenarios (e.g. 14 days).
    target_expiry: if set (YYYYMMDD), filter candidates to ONLY this expiry.
        Used by the strangle leg in put_seller to match the just-sold put's
        expiry exactly. Other callers leave it None and get the best
        candidate across all expiries in the cc_dte_min..max range.
    Uses Black-Scholes theoretical pricing from historical IV data.
    """
    from src.broker.connection import ensure_main_event_loop
    ensure_main_event_loop()
    cfg = get_settings().strategy
    stk_exchange = stock_exchange or exchange
    dte_max = max_dte_override if max_dte_override is not None else cfg.cc_dte_max

    contracts = get_call_contracts(
        symbol,
        exchange=exchange,
        currency=currency,
        min_dte=cfg.cc_dte_min,
        max_dte=dte_max,
        min_strike=min_strike,
    )

    if not contracts:
        log.debug("no_call_contracts", symbol=symbol)
        return None

    stock_price = stock_price_override if stock_price_override and stock_price_override > 0 else get_stock_price(symbol, exchange=stk_exchange, currency=currency)
    if not stock_price or stock_price <= 0:
        log.debug("no_stock_price_for_call_screening", symbol=symbol)
        return None

    if not _passes_min_price(stock_price, currency):
        return None

    ib = get_ib()
    iv = get_current_iv(ib, symbol, exchange=stk_exchange, currency=currency)
    if not iv or iv <= 0:
        log.debug("no_iv_for_call_screening", symbol=symbol)
        return None

    log.info("bs_screening_calls", symbol=symbol, price=round(stock_price, 2),
             iv=round(iv, 3), contracts=len(contracts))

    today = datetime.now().date()
    cc_delta_min = delta_min_override if delta_min_override is not None else cfg.cc_delta_min
    cc_delta_max = delta_max_override if delta_max_override is not None else cfg.cc_delta_max
    candidates = score_call_candidates(stock_price, iv, contracts, cfg, cc_delta_min, cc_delta_max, today)

    # Strangle leg uses target_expiry to match the just-sold put's expiry.
    if target_expiry is not None:
        candidates = [c for c in candidates if c.expiry == target_expiry]

    if not candidates:
        log.debug("no_qualifying_calls", symbol=symbol)
        return None

    # Try top 2 candidates by score; return first that gets live quote AND passes fee floor.
    # BS is for selection only -- never return BS prices as order prices.
    sorted_candidates = sorted(candidates, key=lambda c: c.score, reverse=True)[:2]

    for idx, candidate in enumerate(sorted_candidates):
        try:
            ib = get_ib()
            with get_ib_lock():
                ticker = ib.reqMktData(candidate.contract, "", True, False)
                ib.sleep(2)
                ib.cancelMktData(candidate.contract)

            real_bid = ticker.bid
            real_ask = ticker.ask

            valid_bid = real_bid and real_bid > 0 and real_bid != float('inf') and real_bid != -1.0
            valid_ask = real_ask and real_ask > 0 and real_ask != float('inf') and real_ask != -1.0

            if not valid_bid:
                log.info("call_candidate_no_live_quote_trying_next",
                         symbol=symbol, strike=candidate.strike, rank=idx + 1)
                continue

            real_mid = round((real_bid + real_ask) / 2, 2) if valid_ask else real_bid

            best = ScoredContract(
                contract=candidate.contract,
                strike=candidate.strike,
                expiry=candidate.expiry,
                delta=candidate.delta,
                bid=round(real_bid, 2),
                ask=round(real_ask, 2) if valid_ask else candidate.ask,
                mid=real_mid,
                iv=candidate.iv,
                open_interest=candidate.open_interest,
                score=candidate.score,
            )

            if not _passes_fee_floor(best.bid, 100, currency, 1):
                log.info("call_candidate_fails_fee_floor_trying_next",
                         symbol=symbol, strike=best.strike, bid=best.bid, rank=idx + 1)
                continue

            log.info(
                "call_screened",
                symbol=symbol,
                exchange=exchange,
                strike=best.strike,
                expiry=best.expiry,
                delta=round(best.delta, 3),
                mid=round(best.mid, 2),
                iv=round(best.iv, 3),
                rank=idx + 1,
            )
            return best

        except Exception as e:
            log.warning("call_candidate_quote_exception",
                        symbol=symbol, strike=candidate.strike, error=str(e))
            continue

    log.info("call_candidates_exhausted_no_live",
             symbol=symbol, candidates_tried=len(sorted_candidates))
    return None
