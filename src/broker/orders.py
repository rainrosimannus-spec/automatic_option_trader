"""
Order management — place, modify, cancel orders via IBKR.

Uses the MAIN IBKR connection (same as scans and market data).
All access serialized through get_ib_lock() to prevent conflicts.
"""
from __future__ import annotations

import threading
from typing import Optional

from ib_insync import (
    IB, LimitOrder, MarketOrder, Option, Stock, Trade as IBTrade,
)

from src.broker.connection import get_ib, get_ib_lock, is_connected
from src.core.logger import get_logger

log = get_logger(__name__)


def get_whatif_margin(
    symbol: str,
    expiry: str,
    strike: float,
    right: str = "P",
    quantity: int = 1,
    limit_price: float = 0.0,
    exchange: str = "SMART",
    currency: str = "USD",
) -> float | None:
    """
    Use IBKR whatIfOrder to get actual initial margin for a short option.
    Returns initial margin change (absolute), or None if unavailable.
    Does NOT place an order.
    """
    try:
        with get_ib_lock():
            ib = get_ib()
            contract = Option(symbol, expiry, strike, right, exchange, currency=currency)
            contract.multiplier = "100"
            order = LimitOrder("SELL", quantity, limit_price or 0.01)
            order_state = ib.whatIfOrder(contract, order)
            if order_state and hasattr(order_state, "initMarginChange"):
                val = order_state.initMarginChange
                if val and str(val) not in ("", "1.7976931348623157E308"):
                    margin = abs(float(val))
                    log.info("whatif_margin", symbol=symbol, strike=strike,
                             margin=round(margin, 0))
                    return margin
    except Exception as e:
        log.warning("whatif_margin_error", symbol=symbol, error=str(e) or repr(e))
    return None


def sell_put(
    symbol: str,
    expiry: str,
    strike: float,
    quantity: int = 1,
    limit_price: Optional[float] = None,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[IBTrade]:
    """Sell to open a put option."""
    with get_ib_lock():
        ib = get_ib()
        contract = Option(symbol, expiry, strike, "P", exchange, currency=currency)
        contract.multiplier = "100"

        if limit_price:
            order = LimitOrder("SELL", quantity, limit_price)
        else:
            order = MarketOrder("SELL", quantity)

        order.tif = "DAY"
        order.outsideRth = False

        log.info("placing_sell_put",
                 symbol=symbol, strike=strike, expiry=expiry,
                 qty=quantity, limit=limit_price,
                 exchange=exchange, currency=currency)

        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        status = trade.orderStatus.status
        log_msg = ""
        if trade.log:
            log_msg = trade.log[-1].message if trade.log[-1].message else ""

        log.info("order_status_after_place", symbol=symbol, strike=strike,
                 expiry=expiry, status=status, message=log_msg[:200])

        if status in ("Cancelled", "Inactive"):
            log.error("order_rejected", symbol=symbol, strike=strike,
                      expiry=expiry, status=status, reason=log_msg[:200])
            return None

        if status == "PreSubmitted":
            log.warning("order_presubmitted_may_not_execute", symbol=symbol,
                        strike=strike, status=status, message=log_msg[:200])

        return trade


def buy_to_close_put(
    symbol: str,
    expiry: str,
    strike: float,
    quantity: int = 1,
    limit_price: Optional[float] = None,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[IBTrade]:
    """Buy to close a short put position."""
    with get_ib_lock():
        ib = get_ib()
        contract = Option(symbol, expiry, strike, "P", exchange, currency=currency)
        ib.qualifyContracts(contract)

        if limit_price:
            order = LimitOrder("BUY", quantity, limit_price)
        else:
            order = MarketOrder("BUY", quantity)

        order.tif = "DAY"

        log.info("placing_buy_put_close",
                 symbol=symbol, strike=strike, expiry=expiry,
                 qty=quantity, exchange=exchange)

        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        status = trade.orderStatus.status
        log_msg = ""
        if trade.log:
            log_msg = trade.log[-1].message if trade.log[-1].message else ""
        log.info("order_status_after_place", symbol=symbol, strike=strike,
                 expiry=expiry, status=status, action="buy_close_put",
                 message=log_msg[:200])

        if status in ("Cancelled", "Inactive"):
            log.error("order_rejected", symbol=symbol, strike=strike,
                      expiry=expiry, status=status, reason=log_msg[:200])
            return None

        return trade



def buy_to_close_call(
    symbol: str,
    expiry: str,
    strike: float,
    quantity: int = 1,
    limit_price: Optional[float] = None,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[IBTrade]:
    """Buy to close a short covered call position."""
    with get_ib_lock():
        ib = get_ib()
        contract = Option(symbol, expiry, strike, "C", exchange, currency=currency)
        ib.qualifyContracts(contract)

        if limit_price:
            order = LimitOrder("BUY", quantity, limit_price)
        else:
            order = MarketOrder("BUY", quantity)

        order.tif = "DAY"

        log.info("placing_buy_call_close",
                 symbol=symbol, strike=strike, expiry=expiry,
                 qty=quantity, exchange=exchange)

        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        status = trade.orderStatus.status
        log_msg = ""
        if trade.log:
            log_msg = trade.log[-1].message if trade.log[-1].message else ""
        log.info("order_status_after_place", symbol=symbol, strike=strike,
                 expiry=expiry, status=status, action="buy_close_call",
                 message=log_msg[:200])

        if status in ("Cancelled", "Inactive"):
            log.error("order_rejected", symbol=symbol, strike=strike,
                      expiry=expiry, status=status, reason=log_msg[:200])
            return None

        return trade

def sell_covered_call(
    symbol: str,
    expiry: str,
    strike: float,
    quantity: int = 1,
    limit_price: Optional[float] = None,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[IBTrade]:
    """Sell to open a covered call."""
    with get_ib_lock():
        ib = get_ib()
        contract = Option(symbol, expiry, strike, "C", exchange, currency=currency)
        ib.qualifyContracts(contract)

        if limit_price:
            order = LimitOrder("SELL", quantity, limit_price)
        else:
            order = MarketOrder("SELL", quantity)

        order.tif = "DAY"
        order.outsideRth = False

        log.info("placing_sell_call",
                 symbol=symbol, strike=strike, expiry=expiry,
                 qty=quantity, limit=limit_price,
                 exchange=exchange, currency=currency)

        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        status = trade.orderStatus.status
        log_msg = ""
        if trade.log:
            log_msg = trade.log[-1].message if trade.log[-1].message else ""
        log.info("order_status_after_place", symbol=symbol, strike=strike,
                 expiry=expiry, status=status, action="sell_call",
                 message=log_msg[:200])

        if status in ("Cancelled", "Inactive"):
            log.error("order_rejected", symbol=symbol, strike=strike,
                      expiry=expiry, status=status, reason=log_msg[:200])
            return None

        return trade


def cancel_order(trade: IBTrade) -> None:
    """Cancel an open order."""
    with get_ib_lock():
        ib = get_ib()
        ib.cancelOrder(trade.order)
        log.info("order_cancelled", order_id=trade.order.orderId)


def get_open_orders() -> list[IBTrade]:
    """Get all open/pending orders."""
    with get_ib_lock():
        ib = get_ib()
        return ib.openTrades()


# ── Cached open orders for dashboard (non-blocking) ──
_cached_orders: list = []
_cache_lock = threading.Lock()


def refresh_open_orders_cache() -> None:
    """Refresh the open orders cache. Called by health check job.
    Uses the main connection."""
    global _cached_orders
    try:
        if not is_connected():
            return
        with get_ib_lock():
            ib = get_ib()
            ib.reqAllOpenOrders()
            ib.sleep(2)
            trades = ib.openTrades()
            new_cache = []
            for oo in trades:
                c = oo.contract
                new_cache.append({
                    "order_id": oo.order.orderId,
                    "symbol": getattr(c, 'symbol', '?'),
                    "action": oo.order.action,
                    "qty": int(oo.order.totalQuantity),
                    "limit": oo.order.lmtPrice if hasattr(oo.order, 'lmtPrice') else None,
                    "strike": getattr(c, 'strike', None),
                    "expiry": getattr(c, 'lastTradeDateOrContractMonth', None),
                    "right": getattr(c, 'right', None),
                    "status": oo.orderStatus.status,
                })
            with _cache_lock:
                _cached_orders = new_cache
    except Exception:
        pass
    finally:
        # If not connected or failed, clear stale cache so dashboard shows empty
        if not is_connected():
            with _cache_lock:
                _cached_orders = []


def get_cached_open_orders() -> list:
    """Return cached open orders (non-blocking, for dashboard)."""
    with _cache_lock:
        return list(_cached_orders)
