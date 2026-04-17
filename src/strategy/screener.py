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
from src.broker.connection import get_ib
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

    candidates: list[ScoredContract] = []

    # Use dynamic delta if provided, else fall back to config
    delta_min = delta_override[0] if delta_override else cfg.delta_min
    delta_max = delta_override[1] if delta_override else cfg.delta_max

    today = datetime.now().date()

    for contract in contracts:
        exp_date = datetime.strptime(contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
        dte = (exp_date - today).days
        if dte < 0:
            continue  # only skip expired options, not same-day

        # For DTE 0, use a small T so BS doesn't divide by zero
        T = max(dte, 0.25) / 365.0  # minimum ~6 hours of time value

        greeks = compute_put_greeks(stock_price, contract.strike, T, iv)
        if not greeks:
            continue

        delta = abs(greeks.delta)
        bid = greeks.bid
        ask = greeks.ask
        mid = greeks.mid

        # ── 0-4 DTE: fallback path — use strike distance instead of delta ──
        # BS delta is unreliable near expiry (gamma extremes).
        # Instead, filter by how far OTM the strike is as % of stock price.
        # Target: 3-10% OTM (e.g., stock at $186 → strikes $167-$180)
        if dte <= 3:
            otm_pct = (stock_price - contract.strike) / stock_price
            if otm_pct < 0.02 or otm_pct > 0.12:
                continue  # skip <2% OTM (too risky) or >12% OTM (no premium)
            eff_min_premium = max(getattr(cfg, 'min_premium_put', cfg.min_premium), 0.05)
            eff_min_bid = max(cfg.min_bid, 0.03)

            if mid < eff_min_premium:
                continue
            if bid < eff_min_bid:
                continue

            # ── Scoring components ──
            # 1. OTM distance: closer to 5% OTM = better (0-1)
            otm_target = 0.05
            otm_score = 1 - abs(otm_pct - otm_target) / otm_target
            otm_score = max(0, min(1, otm_score))

            # 2. Return on margin: premium relative to capital at risk (0-1)
            # Margin ~= 20% of strike * 100 (IBKR standard for short puts)
            margin_required = contract.strike * 100 * 0.20
            if margin_required > 0:
                rom = (mid * 100) / margin_required  # e.g. $0.50 premium / $3600 margin = 0.014
                # Normalize: 0.5% return = 0, 3%+ return = 1
                rom_score = min(1.0, max(0, (rom - 0.005) / 0.025))
            else:
                rom_score = 0

            # 3. Premium relative to stock price (0-1)
            # Normalizes across different price levels
            # 0.1% of stock price = 0, 1%+ = 1
            prem_pct = mid / stock_price if stock_price > 0 else 0
            premium_score = min(1.0, max(0, prem_pct / 0.01))

            # Final score: capital efficiency matters most
            # 35% OTM distance + 35% return-on-margin + 25% premium + 5% base
            dte_score = (3 - dte) / 3  # 0DTE=1.0, 1DTE=0.67, 2DTE=0.33, 3DTE=0.0
            score = (otm_score * 0.30) + (rom_score * 0.30) + (premium_score * 0.25) + (dte_score * 0.10) + 0.05

            candidates.append(ScoredContract(
                contract=contract,
                strike=contract.strike,
                expiry=contract.lastTradeDateOrContractMonth,
                delta=delta,  # keep BS delta for display even if unreliable
                bid=bid,
                ask=ask,
                mid=mid,
                iv=iv,
                open_interest=0,
                score=score,
            ))
            continue

        # ── 5-14 DTE: primary path (7 DTE target strategy) ──
        # Delta is reliable here — use it as the main filter.
        # Scoring prefers contracts closest to 7 DTE, good delta placement,
        # capital efficiency (ROM), and reasonable premium.
        if delta < delta_min or delta > delta_max:
            continue
        if mid < getattr(cfg, 'min_premium_put', cfg.min_premium):
            continue
        if bid < cfg.min_bid:
            continue

        # 1. DTE score — prefer midpoint of allowed DTE range, penalise both shorter and longer
        #    Midpoint of resolved_dte_min/max — no longer hardcoded to 7 DTE
        dte_target = max(1, (resolved_dte_min + resolved_dte_max) // 2)
        dte_score = 1 - abs(dte - dte_target) / max(dte_target, 1)
        dte_score = max(0.0, min(1.0, dte_score))

        # 2. Delta score — prefer centre of allowed range (~0.20-0.25)
        target_delta = (delta_min + delta_max) / 2
        delta_score = 1 - abs(delta - target_delta) / target_delta
        delta_score = max(0.0, min(1.0, delta_score))

        # 3. Return on margin — premium / capital at risk
        #    Normalised: 0.5% = 0, 3%+ = 1
        margin_required = contract.strike * 100 * 0.20
        rom = (mid * 100) / margin_required if margin_required > 0 else 0
        rom_score = min(1.0, max(0.0, (rom - 0.005) / 0.025))

        # 4. Premium as % of stock price — reduced weight vs 0-3 DTE
        #    Avoids chasing high premium from risky near-ATM strikes
        #    Normalised: 0.1% = 0, 1%+ = 1
        prem_pct = mid / stock_price if stock_price > 0 else 0
        premium_score = min(1.0, max(0.0, prem_pct / 0.01))

        # Final score: DTE proximity + delta quality + capital efficiency + premium
        score = (dte_score * 0.25) + (delta_score * 0.30) + (rom_score * 0.30) + (premium_score * 0.15)

        candidates.append(ScoredContract(
            contract=contract,
            strike=contract.strike,
            expiry=contract.lastTradeDateOrContractMonth,
            delta=delta,
            bid=bid,
            ask=ask,
            mid=mid,
            iv=iv,
            open_interest=0,
            score=score,
        ))

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
) -> Optional[ScoredContract]:
    """
    Screen and rank call contracts for covered call writing.
    delta_min/max_override: for progressive strike management.
    stock_price_override: use live price instead of get_stock_price (yesterday close).
    max_dte_override: cap DTE for roll-up scenarios (e.g. 14 days).
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

    candidates: list[ScoredContract] = []
    cc_delta_min = delta_min_override if delta_min_override is not None else cfg.cc_delta_min
    cc_delta_max = delta_max_override if delta_max_override is not None else cfg.cc_delta_max

    today = datetime.now().date()

    for contract in contracts:
        exp_date = datetime.strptime(contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
        dte = (exp_date - today).days
        if dte <= 0:
            continue

        T = dte / 365.0

        greeks = compute_call_greeks(stock_price, contract.strike, T, iv)
        if not greeks:
            continue

        delta = abs(greeks.delta)
        bid = greeks.bid
        ask = greeks.ask
        mid = greeks.mid

        if delta < cc_delta_min or delta > cc_delta_max:
            continue
        if mid < cfg.min_premium:
            continue
        if bid < cfg.min_bid:
            continue

        target_delta = (cc_delta_min + cc_delta_max) / 2
        delta_score = 1 - abs(delta - target_delta) / target_delta
        premium_score = mid
        score = (delta_score * 0.4) + (premium_score * 0.6)

        candidates.append(ScoredContract(
            contract=contract,
            strike=contract.strike,
            expiry=contract.lastTradeDateOrContractMonth,
            delta=delta,
            bid=bid,
            ask=ask,
            mid=mid,
            iv=iv,
            open_interest=0,
            score=score,
        ))

    if not candidates:
        log.debug("no_qualifying_calls", symbol=symbol)
        return None

    # Try top 2 candidates by score; return first that gets live quote AND passes fee floor.
    # BS is for selection only -- never return BS prices as order prices.
    sorted_candidates = sorted(candidates, key=lambda c: c.score, reverse=True)[:2]

    for idx, candidate in enumerate(sorted_candidates):
        try:
            ib = get_ib()
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
