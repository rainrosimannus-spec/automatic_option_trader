"""
IPO Rider — execution engine.

Phase 1 (Day-one flip):
  1. Poll IBKR every 30s checking if ticker is tradeable
  2. Once found: wait for first trade to print (opening auction done)
  3. Buy market order
  4. Place trailing stop sell + hard stop-loss
  5. Monitor until sold

Phase 2 (Lockup re-entry):
  1. Record pre-lockup price a few days before lockup expiry
  2. After lockup date: monitor for dip
  3. Place trailing stop buy order (triggers on bounce from low)
  4. Once filled: record as long-term portfolio holding
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from typing import Optional

from ib_insync import IB, Stock, MarketOrder, Order

from src.core.database import get_db
from src.core.logger import get_logger
from src.ipo.models import IpoWatchlist
from src.portfolio.models import PortfolioHolding, PortfolioTransaction

log = get_logger(__name__)


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class IpoTrader:
    """Manages IPO scanning, day-one flips, and lockup re-entries."""

    def __init__(self, ib: IB):
        self.ib = ib

    # ══════════════════════════════════════════════════════════
    # PHASE 1: Day-one flip
    # ══════════════════════════════════════════════════════════

    def scan_for_new_ipos(self):
        """
        Check all 'watching' IPOs to see if their ticker is now tradeable.
        Only scans if:
        - Expected date is set and we're within 7 days of it, OR
        - Expected date is today or in the past
        Called by scheduler every 30 seconds during market hours.
        """
        _ensure_event_loop()
        today = datetime.utcnow().strftime("%Y-%m-%d")

        with get_db() as db:
            watching = db.query(IpoWatchlist).filter(
                IpoWatchlist.status == "watching",
                IpoWatchlist.flip_enabled == True,
            ).all()

        if not watching:
            return

        for ipo in watching:
            # Only scan if we're near the expected IPO date
            if not ipo.expected_date:
                continue  # no date set = don't auto-scan, user must set a date

            # Skip if IPO date is more than 7 days away
            try:
                ipo_date = datetime.strptime(ipo.expected_date, "%Y-%m-%d")
                days_until = (ipo_date - datetime.utcnow()).days
                if days_until > 7:
                    continue  # too early to scan
            except ValueError:
                continue  # invalid date format

            try:
                if self._is_ticker_tradeable(ipo.expected_ticker, ipo.exchange, ipo.currency):
                    log.info("ipo_ticker_found", ticker=ipo.expected_ticker, company=ipo.company_name)
                    self._execute_day_one_buy(ipo)
            except Exception as e:
                log.debug("ipo_scan_check", ticker=ipo.expected_ticker, error=str(e))

    def _is_ticker_tradeable(self, ticker: str, exchange: str, currency: str) -> bool:
        """
        Check if a ticker exists and has traded (opening auction complete).
        Extra careful to avoid matching existing stocks with similar tickers.
        """
        contract = Stock(ticker, exchange, currency)
        qualified = self.ib.qualifyContracts(contract)

        if not qualified:
            return False

        # Verify this is actually a new listing, not an existing stock
        # Check if it has more than 1 day of trading history — if yes, it's NOT a new IPO
        try:
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="",
                durationStr="5 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True,
                formatDate=1, timeout=5,
            )
            if bars and len(bars) > 2:
                # Has multiple days of history — this is an existing stock, not a new IPO
                return False
        except Exception:
            pass  # no history = could be brand new, continue checking

        # Check if there's a last price (meaning first trade has printed = auction done)
        self.ib.reqMarketDataType(1)  # live data only, not delayed
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(3)  # wait for data

        last = ticker_data.last
        self.ib.cancelMktData(contract)

        if last and not math.isnan(last) and last > 0:
            return True

        return False

    def _execute_day_one_buy(self, ipo: IpoWatchlist):
        """Buy shares and set up trailing stop + hard stop-loss."""
        contract = Stock(ipo.expected_ticker, ipo.exchange, ipo.currency)
        self.ib.qualifyContracts(contract)

        # Get current price for position sizing
        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)

        price = ticker_data.last
        if not price or math.isnan(price) or price <= 0:
            price = ticker_data.close
        if not price or math.isnan(price) or price <= 0:
            log.warning("ipo_no_price", ticker=ipo.expected_ticker)
            self.ib.cancelMktData(contract)
            return

        self.ib.cancelMktData(contract)

        # Calculate shares based on configured amount
        shares = int(ipo.flip_amount / price)
        if shares < 1:
            log.warning("ipo_insufficient_funds", ticker=ipo.expected_ticker,
                        price=price, amount=ipo.flip_amount)
            return

        # Check buying power before placing order
        required_amount = price * shares
        try:
            from src.broker.account import get_account_summary
            summary = get_account_summary()
            buying_power = summary.get("buying_power", 0)
            if buying_power < required_amount:
                log.warning("ipo_insufficient_buying_power",
                            ticker=ipo.expected_ticker,
                            required=round(required_amount, 2),
                            available=round(buying_power, 2))
                return
        except Exception as e:
            log.warning("ipo_buying_power_check_failed", error=str(e))
            return  # fail closed — don't buy if we can't verify buying power

        # Place market buy order
        buy_order = MarketOrder("BUY", shares)
        buy_order.tif = "DAY"

        log.info("ipo_day_one_buy", ticker=ipo.expected_ticker,
                 shares=shares, est_price=round(price, 2))

        buy_trade = self.ib.placeOrder(contract, buy_order)
        self.ib.sleep(3)

        # Wait for fill
        for _ in range(20):  # up to 20 seconds
            if buy_trade.orderStatus.status == "Filled":
                break
            self.ib.sleep(1)

        if buy_trade.orderStatus.status != "Filled":
            log.warning("ipo_buy_not_filled", ticker=ipo.expected_ticker,
                        status=buy_trade.orderStatus.status)
            return

        fill_price = buy_trade.orderStatus.avgFillPrice
        log.info("ipo_buy_filled", ticker=ipo.expected_ticker,
                 shares=shares, fill_price=fill_price)

        # Place trailing stop sell order
        trailing_order = Order()
        trailing_order.action = "SELL"
        trailing_order.totalQuantity = shares
        trailing_order.orderType = "TRAIL"
        trailing_order.trailingPercent = ipo.flip_trailing_pct
        trailing_order.tif = "GTC"

        trailing_trade = self.ib.placeOrder(contract, trailing_order)
        self.ib.sleep(1)

        # Place hard stop-loss as separate order
        stop_price = round(fill_price * (1 - ipo.flip_stop_loss_pct / 100), 2)
        stop_order = Order()
        stop_order.action = "SELL"
        stop_order.totalQuantity = shares
        stop_order.orderType = "STP"
        stop_order.auxPrice = stop_price
        stop_order.tif = "GTC"

        stop_trade = self.ib.placeOrder(contract, stop_order)
        self.ib.sleep(1)

        # Update database
        with get_db() as db:
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.status = "ipo_trading"
                entry.flip_entry_price = fill_price
                entry.flip_shares = shares
                entry.flip_entry_time = datetime.utcnow()
                entry.flip_order_id = trailing_trade.order.orderId
                entry.flip_stop_order_id = stop_trade.order.orderId
                entry.updated_at = datetime.utcnow()

    def check_flip_exits(self):
        """
        Check if any day-one trailing stop or stop-loss orders have filled,
        OR if max hold days have been exceeded (force sell at market).
        Called by scheduler periodically.
        """
        _ensure_event_loop()

        with get_db() as db:
            active_flips = db.query(IpoWatchlist).filter(
                IpoWatchlist.status == "ipo_trading",
            ).all()

        if not active_flips:
            return

        for ipo in active_flips:
            try:
                # First check if trailing/stop orders filled
                filled = self._check_flip_filled(ipo)

                # If not filled, check if max hold days exceeded
                if not filled and ipo.flip_entry_time:
                    trading_days = self._count_trading_days(ipo.flip_entry_time, datetime.utcnow())
                    max_days = ipo.flip_max_hold_days if hasattr(ipo, 'flip_max_hold_days') and ipo.flip_max_hold_days else 5

                    if trading_days >= max_days:
                        log.info("ipo_flip_max_hold_reached",
                                 ticker=ipo.expected_ticker,
                                 days=trading_days, max=max_days)
                        self._force_sell_flip(ipo)

            except Exception as e:
                log.error("ipo_flip_check_error", ticker=ipo.expected_ticker, error=str(e))

    def _count_trading_days(self, start: datetime, end: datetime) -> int:
        """Count weekdays (trading days) between two dates."""
        count = 0
        current = start
        while current.date() < end.date():
            current += timedelta(days=1)
            if current.weekday() < 5:  # Mon=0 to Fri=4
                count += 1
        return count

    def _force_sell_flip(self, ipo: IpoWatchlist):
        """Cancel existing orders and sell at market (max hold days exceeded)."""
        contract = Stock(ipo.expected_ticker, ipo.exchange, ipo.currency)
        self.ib.qualifyContracts(contract)

        # Cancel trailing stop and hard stop-loss
        open_trades = self.ib.openTrades()
        for t in open_trades:
            if t.order.orderId in (ipo.flip_order_id, ipo.flip_stop_order_id):
                if t.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
                    try:
                        self.ib.cancelOrder(t.order)
                        self.ib.sleep(1)
                    except Exception:
                        pass

        # Sell at market
        sell_order = MarketOrder("SELL", ipo.flip_shares)
        sell_order.tif = "DAY"

        log.info("ipo_flip_force_sell", ticker=ipo.expected_ticker,
                 shares=ipo.flip_shares)

        sell_trade = self.ib.placeOrder(contract, sell_order)
        self.ib.sleep(3)

        # Wait for fill
        for _ in range(20):
            if sell_trade.orderStatus.status == "Filled":
                break
            self.ib.sleep(1)

        if sell_trade.orderStatus.status == "Filled":
            exit_price = sell_trade.orderStatus.avgFillPrice
            self._record_flip_exit(ipo, exit_price, f"IPO max hold ({ipo.flip_max_hold_days}d) market sell")
        else:
            log.warning("ipo_force_sell_not_filled", ticker=ipo.expected_ticker,
                        status=sell_trade.orderStatus.status)

    def _check_flip_filled(self, ipo: IpoWatchlist) -> bool:
        """Check if trailing stop or hard stop-loss has been filled. Returns True if sold."""
        # Check all open orders for this account
        open_trades = self.ib.openTrades()
        executions = self.ib.fills()

        trailing_filled = False
        exit_price = None

        # Check if trailing stop order filled
        for fill in executions:
            if (fill.contract.symbol == ipo.expected_ticker
                    and fill.execution.side == "SLD"):
                exit_price = fill.execution.price
                trailing_filled = True
                break

        # Also check via order status
        if not trailing_filled:
            for t in open_trades:
                if t.order.orderId in (ipo.flip_order_id, ipo.flip_stop_order_id):
                    if t.orderStatus.status == "Filled":
                        exit_price = t.orderStatus.avgFillPrice
                        trailing_filled = True

        if not trailing_filled or not exit_price:
            return False

        # Cancel the other order
        for t in open_trades:
            if t.order.orderId in (ipo.flip_order_id, ipo.flip_stop_order_id):
                if t.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
                    try:
                        self.ib.cancelOrder(t.order)
                    except Exception:
                        pass

        self._record_flip_exit(ipo, exit_price, "IPO trailing/stop exit")
        return True

    def _record_flip_exit(self, ipo: IpoWatchlist, exit_price: float, exit_reason: str):
        """Record the flip exit in trades and update IPO status."""
        pnl = (exit_price - ipo.flip_entry_price) * ipo.flip_shares

        log.info("ipo_flip_sold", ticker=ipo.expected_ticker,
                 entry=ipo.flip_entry_price, exit=exit_price,
                 shares=ipo.flip_shares, pnl=round(pnl, 2),
                 reason=exit_reason)

        # Record as trade in options positions (short-term speculative)
        from src.core.models import Trade, Position, TradeType, OrderStatus, PositionStatus
        with get_db() as db:
            # Create position
            pos = Position(
                symbol=ipo.expected_ticker,
                status=PositionStatus.CLOSED,
                position_type="ipo_flip",
                entry_premium=ipo.flip_entry_price,
                cost_basis=ipo.flip_entry_price,
                quantity=ipo.flip_shares,
                realized_pnl=pnl,
                opened_at=ipo.flip_entry_time,
                closed_at=datetime.utcnow(),
            )
            db.add(pos)
            db.flush()

            # Buy trade
            db.add(Trade(
                position_id=pos.id,
                symbol=ipo.expected_ticker,
                trade_type=TradeType.BUY_STOCK,
                strike=0, expiry="", premium=0,
                quantity=ipo.flip_shares,
                fill_price=ipo.flip_entry_price,
                order_status=OrderStatus.FILLED,
                source="ipo_rider",
                notes=f"IPO day-one buy: {ipo.company_name}",
                created_at=ipo.flip_entry_time,
            ))

            # Sell trade
            db.add(Trade(
                position_id=pos.id,
                symbol=ipo.expected_ticker,
                trade_type=TradeType.SELL_STOCK,
                strike=0, expiry="", premium=0,
                quantity=ipo.flip_shares,
                fill_price=exit_price,
                order_status=OrderStatus.FILLED,
                source="ipo_rider",
                notes=f"IPO trailing stop exit: {ipo.company_name}",
                created_at=datetime.utcnow(),
            ))

            # Update IPO entry
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                if entry.lockup_enabled and entry.lockup_date:
                    entry.status = "lockup_waiting"
                else:
                    entry.status = "flip_done"
                entry.flip_exit_price = exit_price
                entry.flip_exit_time = datetime.utcnow()
                entry.flip_pnl = pnl
                entry.updated_at = datetime.utcnow()

        # Send alert
        try:
            from src.core.alerts import get_alert_manager
            get_alert_manager().send(
                title=f"🚀 IPO Flip: {ipo.expected_ticker}",
                body=(
                    f"{ipo.company_name}\n"
                    f"Entry: ${ipo.flip_entry_price:.2f} → Exit: ${exit_price:.2f}\n"
                    f"P&L: ${pnl:+,.2f} ({pnl / (ipo.flip_entry_price * ipo.flip_shares) * 100:+.1f}%)\n"
                    f"Shares: {ipo.flip_shares}"
                ),
                priority="high",
                tags="ipo",
            )
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # PHASE 2: Lockup re-entry
    # ══════════════════════════════════════════════════════════

    def check_lockup_entries(self):
        """
        Monitor IPOs approaching or past lockup expiry.
        - Record pre-lockup price 3 days before
        - After lockup: place trailing stop buy when dip target reached
        Called by scheduler daily.
        """
        _ensure_event_loop()
        today = datetime.utcnow().strftime("%Y-%m-%d")

        with get_db() as db:
            lockup_waiting = db.query(IpoWatchlist).filter(
                IpoWatchlist.status.in_(["lockup_waiting", "flip_done"]),
                IpoWatchlist.lockup_enabled == True,
                IpoWatchlist.lockup_date.isnot(None),
            ).all()

        for ipo in lockup_waiting:
            try:
                self._process_lockup(ipo, today)
            except Exception as e:
                log.error("ipo_lockup_check_error", ticker=ipo.expected_ticker, error=str(e))

        # Also check active lockup trailing buy orders
        with get_db() as db:
            lockup_trading = db.query(IpoWatchlist).filter(
                IpoWatchlist.status == "lockup_trading",
            ).all()

        for ipo in lockup_trading:
            try:
                self._check_lockup_fill(ipo)
            except Exception as e:
                log.error("ipo_lockup_fill_check_error", ticker=ipo.expected_ticker, error=str(e))

    def _process_lockup(self, ipo: IpoWatchlist, today: str):
        """Handle pre-lockup price recording and post-lockup trailing buy."""
        lockup = ipo.lockup_date
        if not lockup:
            return

        # Record pre-lockup price 3 days before lockup
        from datetime import datetime as dt
        lockup_dt = dt.strptime(lockup, "%Y-%m-%d")
        pre_lockup_start = (lockup_dt - timedelta(days=3)).strftime("%Y-%m-%d")

        if today >= pre_lockup_start and not ipo.pre_lockup_price:
            price = self._get_current_price(ipo.expected_ticker, ipo.exchange, ipo.currency)
            if price and price > 0:
                with get_db() as db:
                    entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
                    if entry:
                        entry.pre_lockup_price = price
                        entry.updated_at = datetime.utcnow()
                log.info("ipo_pre_lockup_price_recorded", ticker=ipo.expected_ticker, price=price)

        # After lockup date: check if dip target reached, place trailing buy
        if today >= lockup and ipo.pre_lockup_price:
            price = self._get_current_price(ipo.expected_ticker, ipo.exchange, ipo.currency)
            if not price or price <= 0:
                return

            dip_pct = ((ipo.pre_lockup_price - price) / ipo.pre_lockup_price) * 100

            if dip_pct >= ipo.lockup_dip_pct:
                log.info("ipo_lockup_dip_reached", ticker=ipo.expected_ticker,
                         pre_lockup=ipo.pre_lockup_price, current=price,
                         dip_pct=round(dip_pct, 1))
                self._place_lockup_trailing_buy(ipo, price)

    def _place_lockup_trailing_buy(self, ipo: IpoWatchlist, current_price: float):
        """Place trailing stop buy order to catch the bounce after lockup dip."""
        contract = Stock(ipo.expected_ticker, ipo.exchange, ipo.currency)
        self.ib.qualifyContracts(contract)

        shares = int(ipo.lockup_amount / current_price)
        if shares < 1:
            log.warning("ipo_lockup_insufficient_funds", ticker=ipo.expected_ticker)
            return

        # Check buying power before placing order
        required_amount = current_price * shares
        try:
            from src.broker.account import get_account_summary
            summary = get_account_summary()
            buying_power = summary.get("buying_power", 0)
            if buying_power < required_amount:
                log.warning("ipo_lockup_insufficient_buying_power",
                            ticker=ipo.expected_ticker,
                            required=round(required_amount, 2),
                            available=round(buying_power, 2))
                return
        except Exception as e:
            log.warning("ipo_lockup_buying_power_check_failed", error=str(e))
            return  # fail closed

        # Trailing stop buy: triggers when price bounces up by trailing_buy_pct from low
        trailing_buy = Order()
        trailing_buy.action = "BUY"
        trailing_buy.totalQuantity = shares
        trailing_buy.orderType = "TRAIL"
        trailing_buy.trailingPercent = ipo.lockup_trailing_buy_pct
        trailing_buy.tif = "GTC"

        log.info("ipo_lockup_trailing_buy", ticker=ipo.expected_ticker,
                 shares=shares, trailing_pct=ipo.lockup_trailing_buy_pct)

        trade = self.ib.placeOrder(contract, trailing_buy)
        self.ib.sleep(1)

        with get_db() as db:
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.status = "lockup_trading"
                entry.lockup_shares = shares
                entry.lockup_order_id = trade.order.orderId
                entry.updated_at = datetime.utcnow()

    def _check_lockup_fill(self, ipo: IpoWatchlist):
        """Check if lockup trailing buy has been filled."""
        executions = self.ib.fills()

        fill_price = None
        for fill in executions:
            if (fill.contract.symbol == ipo.expected_ticker
                    and fill.execution.side == "BOT"):
                fill_price = fill.execution.price
                break

        # Also check order status directly
        if not fill_price:
            open_trades = self.ib.openTrades()
            for t in open_trades:
                if t.order.orderId == ipo.lockup_order_id:
                    if t.orderStatus.status == "Filled":
                        fill_price = t.orderStatus.avgFillPrice

        if not fill_price:
            return

        log.info("ipo_lockup_buy_filled", ticker=ipo.expected_ticker,
                 price=fill_price, shares=ipo.lockup_shares)

        # Record as long-term portfolio holding
        with get_db() as db:
            # Update IPO entry
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.status = "lockup_done"
                entry.lockup_entry_price = fill_price
                entry.lockup_entry_time = datetime.utcnow()
                entry.updated_at = datetime.utcnow()

            # Create portfolio holding
            existing = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == ipo.expected_ticker,
            ).first()

            if existing:
                total_cost = existing.avg_cost * existing.shares + fill_price * ipo.lockup_shares
                existing.shares += ipo.lockup_shares
                existing.avg_cost = total_cost / existing.shares if existing.shares > 0 else 0
                existing.total_invested += fill_price * ipo.lockup_shares
                existing.last_bought = datetime.utcnow()
            else:
                db.add(PortfolioHolding(
                    symbol=ipo.expected_ticker,
                    name=ipo.company_name,
                    exchange=ipo.exchange,
                    currency=ipo.currency,
                    tier="growth",
                    shares=ipo.lockup_shares,
                    avg_cost=fill_price,
                    total_invested=fill_price * ipo.lockup_shares,
                    current_price=fill_price,
                    market_value=fill_price * ipo.lockup_shares,
                    entry_method="ipo_lockup",
                ))

            # Record transaction
            db.add(PortfolioTransaction(
                symbol=ipo.expected_ticker,
                action="buy",
                shares=ipo.lockup_shares,
                price=fill_price,
                amount=fill_price * ipo.lockup_shares,
                tier="growth",
                signal="ipo_lockup_reentry",
                source="ipo_rider",
                notes=f"IPO lockup re-entry: {ipo.company_name}",
            ))

        # Send alert
        try:
            from src.core.alerts import get_alert_manager
            get_alert_manager().send(
                title=f"🔄 IPO Lockup Re-entry: {ipo.expected_ticker}",
                body=(
                    f"{ipo.company_name}\n"
                    f"Entry price: ${fill_price:.2f}\n"
                    f"Pre-lockup was: ${ipo.pre_lockup_price:.2f}\n"
                    f"Shares: {ipo.lockup_shares}\n"
                    f"Added to long-term portfolio"
                ),
                priority="high",
                tags="ipo",
            )
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════

    def _get_current_price(self, ticker: str, exchange: str, currency: str) -> Optional[float]:
        """Get current price via historical data."""
        contract = Stock(ticker, exchange, currency)
        try:
            self.ib.qualifyContracts(contract)
            contract.exchange = "SMART"
            for what in ("TRADES", "MIDPOINT"):
                try:
                    bars = self.ib.reqHistoricalData(
                        contract, endDateTime="",
                        durationStr="2 D", barSizeSetting="1 day",
                        whatToShow=what, useRTH=False,
                        formatDate=1, timeout=8,
                    )
                    if bars:
                        return float(bars[-1].close)
                except Exception:
                    pass
                self.ib.sleep(0.5)
        except Exception:
            pass
        return None
