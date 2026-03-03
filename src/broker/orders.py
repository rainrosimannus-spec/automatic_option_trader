"""
Order management — place, modify, cancel orders via IBKR.

Uses a DEDICATED IBKR connection (client ID 13) for order execution,
separate from the scanner connections (50/51/52) and the main connection (12).
This prevents conflicts when auto-approve executes during an active scan.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Optional

from ib_insync import (
    IB, LimitOrder, MarketOrder, Option, Stock, Trade as IBTrade,
)

from src.core.config import get_settings
from src.broker.connection import is_port_open
from src.core.logger import get_logger

log = get_logger(__name__)

_order_ib: Optional[IB] = None
_order_lock = threading.Lock()
ORDER_CLIENT_ID = 13  # Dedicated client ID for order execution


def _get_order_connection() -> IB:
    """Get or create a dedicated IB connection for order execution.
    Separate from scanner and main connections to avoid conflicts."""
    global _order_ib

    # Ensure event loop exists in this thread (AnyIO/uvicorn compatible)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if _order_ib is not None and _order_ib.isConnected():
        return _order_ib

    cfg = get_settings().ibkr
    if not is_port_open(cfg.host, cfg.port):
        raise ConnectionError(f"TWS not reachable on {cfg.host}:{cfg.port}")

    _order_ib = IB()
    _order_ib.connect(
        host=cfg.host,
        port=cfg.port,
        clientId=ORDER_CLIENT_ID,
        timeout=cfg.timeout,
        readonly=cfg.readonly,
        account=cfg.account or "",
    )
    _order_ib.reqMarketDataType(4)
    log.info("order_connection_established", clientId=ORDER_CLIENT_ID)
    return _order_ib


def sell_put(
    symbol: str,
    expiry: str,
    strike: float,
    quantity: int = 1,
    limit_price: Optional[float] = None,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Optional[IBTrade]:
    """
    Sell to open a put option.
    Uses limit order if limit_price is provided, else market order.
    """
    with _order_lock:
        ib = _get_order_connection()
        contract = Option(symbol, expiry, strike, "P", exchange, currency=currency)
        ib.qualifyContracts(contract)

        if limit_price:
            order = LimitOrder("SELL", quantity, limit_price)
        else:
            order = MarketOrder("SELL", quantity)

        order.tif = "DAY"
        order.outsideRth = False

        log.info(
            "placing_sell_put",
            symbol=symbol,
            strike=strike,
            expiry=expiry,
            qty=quantity,
            limit=limit_price,
            exchange=exchange,
            currency=currency,
        )

        trade = ib.placeOrder(contract, order)
        ib.sleep(2)  # wait for order status to update

        # Check order status
        status = trade.orderStatus.status
        log_msg = ""
        if trade.log:
            log_msg = trade.log[-1].message if trade.log[-1].message else ""

        log.info("order_status_after_place", symbol=symbol, strike=strike,
                 expiry=expiry, status=status, message=log_msg[:200])

        # Reject: Cancelled, Inactive, or any warning about exchange not open
        if status in ("Cancelled", "Inactive"):
            log.error("order_rejected", symbol=symbol, strike=strike,
                      expiry=expiry, status=status, reason=log_msg[:200])
            return None

        # If order is just "PreSubmitted" and we're outside market hours,
        # it may never execute. Cancel it to be safe.
        if status == "PreSubmitted":
            log.warning("order_presubmitted_may_not_execute", symbol=symbol,
                        strike=strike, status=status, message=log_msg[:200])
            # Don't cancel — let it ride. But the caller (suggestions.py)
            # will mark it as "submitted" so we track it.

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
    with _order_lock:
        ib = _get_order_connection()
        contract = Option(symbol, expiry, strike, "P", exchange, currency=currency)
        ib.qualifyContracts(contract)

        if limit_price:
            order = LimitOrder("BUY", quantity, limit_price)
        else:
            order = MarketOrder("BUY", quantity)

        order.tif = "DAY"

        log.info(
            "placing_buy_put_close",
            symbol=symbol,
            strike=strike,
            expiry=expiry,
            qty=quantity,
            exchange=exchange,
        )

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
    with _order_lock:
        ib = _get_order_connection()
        contract = Option(symbol, expiry, strike, "C", exchange, currency=currency)
        ib.qualifyContracts(contract)

        if limit_price:
            order = LimitOrder("SELL", quantity, limit_price)
        else:
            order = MarketOrder("SELL", quantity)

        order.tif = "DAY"
        order.outsideRth = False

        log.info(
            "placing_sell_call",
            symbol=symbol,
            strike=strike,
            expiry=expiry,
            qty=quantity,
            limit=limit_price,
            exchange=exchange,
            currency=currency,
        )

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
    with _order_lock:
        ib = _get_order_connection()
        ib.cancelOrder(trade.order)
        log.info("order_cancelled", order_id=trade.order.orderId)


def get_open_orders() -> list[IBTrade]:
    """Get all open/pending orders."""
    with _order_lock:
        ib = _get_order_connection()
        return ib.openTrades()
