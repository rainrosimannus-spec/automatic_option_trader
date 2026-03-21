"""
Profit taker — close positions at 55% profit and optionally roll into new entries.
Rolling = close profitable put → immediately scan for a new put on the same stock.
This extracts more premium from the same capital over time.
"""
from __future__ import annotations

from datetime import datetime
import pytz

from src.broker.market_data import get_stock_price
from src.broker.greeks import compute_put_greeks, get_current_iv
from src.broker.orders import buy_to_close_put
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, TradeType, OrderStatus
from src.core.logger import get_logger
from src.strategy.universe import UniverseManager

from ib_insync import Option as IBOption

log = get_logger(__name__)

# Market hours per currency: (timezone, open_hour, close_hour)
_MARKET_HOURS = {
    "USD": ("US/Eastern",      9, 16),
    "CAD": ("US/Eastern",      9, 16),
    "EUR": ("Europe/Berlin",   9, 17),
    "CHF": ("Europe/Berlin",   9, 17),
    "GBP": ("Europe/London",   8, 16),
    "NOK": ("Europe/Berlin",   9, 17),
    "SEK": ("Europe/Berlin",   9, 17),
    "DKK": ("Europe/Berlin",   9, 17),
    "JPY": ("Asia/Tokyo",      9, 15),
    "AUD": ("Australia/Sydney",10, 16),
    "HKD": ("Asia/Hong_Kong",  9, 16),
}


def _is_market_open(currency: str) -> bool:
    """Return True if the market for this currency is currently open."""
    hours = _MARKET_HOURS.get(currency)
    if not hours:
        return True  # unknown currency — assume open
    tz_name, open_h, close_h = hours
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    return now.weekday() < 5 and open_h <= now.hour < close_h


def _minutes_to_market_open(currency: str) -> float:
    """Return minutes until next market open. 0 if market is already open."""
    hours = _MARKET_HOURS.get(currency)
    if not hours:
        return 0.0
    tz_name, open_h, close_h = hours
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)

    if now.weekday() < 5 and open_h <= now.hour < close_h:
        return 0.0  # market is open right now

    # Find next open: today if before open, else tomorrow (skip weekend)
    from datetime import timedelta
    candidate = now.replace(hour=open_h, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    # Skip Saturday (5) and Sunday (6)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return (candidate - now).total_seconds() / 60.0


class ProfitTaker:
    """Monitor open positions and close at profit target, optionally rolling."""

    def __init__(self):
        self.cfg = get_settings().strategy
        self.universe = UniverseManager()

    def check_positions(self) -> list[str]:
        """
        Check all open short puts and close if profit target is met.
        Returns list of symbols where positions were closed.
        """
        if not self.cfg.profit_take_enabled:
            return []

        log.info("profit_check_started")
        closed: list[str] = []

        with get_db() as db:
            open_puts = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "short_put",
                )
                .all()
            )

            for pos in open_puts:
                try:
                    # Skip if market opens more than 60 minutes from now
                    stock = self.universe.get_stock(pos.symbol)
                    currency = stock.currency if stock else "USD"
                    mins_to_open = _minutes_to_market_open(currency)
                    if mins_to_open > 60:
                        log.debug("profit_check_skipped_market_closed",
                                  symbol=pos.symbol, currency=currency,
                                  mins_to_open=round(mins_to_open))
                        continue

                    # Never take profit on 0-3 DTE — let them expire worthless
                    try:
                        from datetime import datetime as _dt
                        exp_date = _dt.strptime(pos.expiry, "%Y%m%d").date()
                        dte = (exp_date - _dt.now().date()).days
                    except Exception:
                        dte = 99  # parse failure: don't skip, let normal logic decide
                    if dte <= 3:
                        log.info("profit_skip_short_dte", symbol=pos.symbol, dte=dte)
                        continue

                    if self._should_close(pos):
                        success = self._close_position(db, pos)
                        if success:
                            closed.append(pos.symbol)
                except Exception as e:
                    log.error("profit_check_error", symbol=pos.symbol, error=str(e))

        # Cancel any open close-orders for positions whose market is now closed
        try:
            with get_db() as db:
                from src.core.models import TradeType, OrderStatus
                submitted_closes = (
                    db.query(Trade)
                    .filter(
                        Trade.trade_type == TradeType.BUY_PUT,
                        Trade.order_status == OrderStatus.SUBMITTED,
                    )
                    .all()
                )
                if submitted_closes:
                    from src.broker.orders import cancel_order
                    from src.broker.connection import get_ib
                    ib = get_ib()
                    live_trades = ib.trades()
                    for t in submitted_closes:
                        pos_currency = "USD"
                        try:
                            from src.strategy.universe import UniverseManager
                            stk = UniverseManager().get_stock(t.symbol)
                            if stk:
                                pos_currency = stk.currency
                        except Exception:
                            pass
                        if not _is_market_open(pos_currency):
                            for lt in live_trades:
                                if lt.order.orderId == t.order_id:
                                    try:
                                        cancel_order(lt)
                                        t.order_status = OrderStatus.CANCELLED
                                        log.info("cancelled_close_order_market_closed",
                                                 symbol=t.symbol, order_id=t.order_id,
                                                 currency=pos_currency)
                                    except Exception as e:
                                        log.warning("cancel_close_order_failed",
                                                    symbol=t.symbol, error=str(e))
                                    break
                    db.commit()
        except Exception as e:
            log.warning("cancel_closed_market_orders_failed", error=str(e))

        # If rolling is enabled, trigger a re-scan on closed symbols
        if self.cfg.roll_enabled and closed:
            self._roll_positions(closed)

        log.info("profit_check_done", closed=closed)
        return closed

    def _should_close(self, pos: Position) -> bool:
        """
        Check if position should be closed.
        Two triggers:
        Profit target hit — close when dynamic % of premium captured.
        DTE<=3: let expire or assign naturally (no close).
        """
        if not pos.strike or not pos.expiry:
            return False

        # Get exchange/currency for this stock
        stock = self.universe.get_stock(pos.symbol)
        exchange = stock.exchange if stock else "SMART"
        currency = stock.currency if stock else "USD"

        exp_date = datetime.strptime(pos.expiry, "%Y%m%d").date()
        dte = (exp_date - datetime.now().date()).days
        if dte <= 0:
            return False

        # Fetch live market ask from IBKR — fall back to BS only if unavailable
        from src.broker.market_data import get_option_live_price
        stock = self.universe.get_stock(pos.symbol)
        opt_exchange = stock.opt_exchange if stock else "SMART"
        live_bid, live_ask = get_option_live_price(
            pos.symbol, pos.expiry, pos.strike, "P", opt_exchange, currency
        )

        if live_ask and live_ask > 0:
            log.debug("profit_check_live_price", symbol=pos.symbol,
                      live_bid=live_bid, live_ask=live_ask)
            current_ask = live_ask
        else:
            log.warning("profit_check_no_live_price_using_bs", symbol=pos.symbol)
            # Fallback to Black-Scholes
            stock_price = get_stock_price(pos.symbol, exchange=exchange, currency=currency)
            if not stock_price:
                return False
            from src.broker.connection import get_ib
            ib = get_ib()
            iv = get_current_iv(ib, pos.symbol, exchange=exchange, currency=currency)
            if not iv or iv <= 0:
                return False
            T = max(dte, 1) / 365.0
            greeks = compute_put_greeks(stock_price, pos.strike, T, iv)
            if not greeks:
                return False
            current_ask = greeks.ask
        entry_premium = pos.entry_premium
        if entry_premium <= 0:
            return False

        # Profit % = (entry - current) / entry
        profit_pct = (entry_premium - current_ask) / entry_premium

        # Dynamic target: 75% early, 65% mid, 50% near, expire at DTE<=3
        if dte > 14:
            target = 0.75
        elif dte > 7:
            target = 0.65
        elif dte > 3:
            target = 0.50
        else:
            log.debug("dte_too_close_letting_expire", symbol=pos.symbol, dte=dte)
            return False

        if profit_pct >= target:
            log.info(
                "profit_target_hit",
                dte=dte,
                target_pct=f"{target:.0%}",
                symbol=pos.symbol,
                entry=round(entry_premium, 2),
                current=round(current_ask, 2),
                profit_pct=f"{profit_pct:.0%}",
            )
            return True
        return False

    def _close_position(self, db, pos: Position) -> bool:
        """Close the position by buying back the put using BS-computed price.
        Cancels any existing open close order first, then places fresh one with current price."""
        stock = self.universe.get_stock(pos.symbol)
        exchange = stock.exchange if stock else "SMART"
        opt_exchange = stock.opt_exchange if stock else "SMART"  # options exchange (e.g. EUREX for German stocks)
        currency = stock.currency if stock else "USD"
        contract_size = stock.contract_size if stock else 100

        # Cancel any existing open close orders for this position
        # so we don't accumulate duplicate orders across scan cycles
        try:
            existing_trades = db.query(Trade).filter(
                Trade.position_id == pos.id,
                Trade.trade_type == TradeType.BUY_PUT,
                Trade.order_status == OrderStatus.SUBMITTED,
            ).all()
            if existing_trades:
                from src.broker.orders import cancel_order
                from src.broker.connection import get_ib
                ib = get_ib()
                for existing in existing_trades:
                    if existing.order_id:
                        try:
                            # Find the live trade object and cancel it
                            live_trades = ib.trades()
                            for lt in live_trades:
                                if lt.order.orderId == existing.order_id:
                                    cancel_order(lt)
                                    log.info("cancelled_existing_close_order",
                                             symbol=pos.symbol, order_id=existing.order_id)
                                    break
                        except Exception as e:
                            log.warning("cancel_existing_close_failed",
                                        symbol=pos.symbol, error=str(e))
                    existing.order_status = OrderStatus.CANCELLED  # superseded by new order
        except Exception as e:
            log.warning("cancel_existing_close_check_failed", symbol=pos.symbol, error=str(e))

        # Use live market ask as limit price — fall back to BS if unavailable
        from src.broker.market_data import get_option_live_price
        live_bid, live_ask = get_option_live_price(
            pos.symbol, pos.expiry or "", pos.strike or 0, "P", opt_exchange, currency
        )

        if live_ask and live_ask > 0:
            ask_price = live_ask
            log.info("profit_taker_using_live_price", symbol=pos.symbol,
                     live_bid=live_bid, live_ask=live_ask)
        else:
            log.warning("profit_taker_skipping_no_live_price",
                        symbol=pos.symbol, strike=pos.strike)
            return False
        ask_price = live_ask

        trade = buy_to_close_put(
            symbol=pos.symbol,
            expiry=pos.expiry or "",
            strike=pos.strike or 0,
            quantity=pos.quantity,
            limit_price=round(ask_price, 2) if ask_price is not None else None,
            exchange=opt_exchange,
            currency=currency,
        )

        if not trade:
            return False

        # Record the close order as SUBMITTED — position stays OPEN until fill confirmed
        # The IBKR trade sync job will update status to FILLED when it actually executes
        trade_record = Trade(
            position_id=pos.id,
            symbol=pos.symbol,
            trade_type=TradeType.BUY_PUT,
            strike=pos.strike or 0,
            expiry=pos.expiry or "",
            premium=ask_price,
            quantity=pos.quantity,
            fill_price=ask_price,
            order_id=trade.order.orderId,
            order_status=OrderStatus.SUBMITTED,
            notes=f"Profit take at {ask_price:.2f} ({self.cfg.profit_take_pct:.0%} target)",
        )
        db.add(trade_record)

        log.info("profit_take_order_placed", symbol=pos.symbol,
                 order_id=trade.order.orderId, limit_price=round(ask_price, 2))
        return True

    def _roll_positions(self, symbols: list[str]) -> None:
        """
        After closing profitable positions, immediately re-scan those symbols
        for new put entries. This redeploys the freed capital.
        """
        from src.strategy.risk import RiskManager
        from src.strategy.put_seller import PutSeller

        log.info("rolling_positions", symbols=symbols)

        risk = RiskManager(self.universe)
        seller = PutSeller(self.universe, risk)

        for symbol in symbols:
            try:
                result = seller._process_symbol(symbol, current_vix=None)
                if result:
                    risk.increment_daily_count()
                    log.info("roll_successful", symbol=symbol)
                else:
                    log.debug("roll_no_entry", symbol=symbol)
            except Exception as e:
                log.error("roll_failed", symbol=symbol, error=str(e))
