"""
IPO Rider — execution engine (v2).

Phase 1 — opening-day "slack" scalp (small/optional; backtest edge is thin, ~break-even):
  Wait for the first daily CLOSE; flip only names that closed >=3% above their open (day-1 supply
  absorbed → buyers control day 2), enter day 2, exit on a trailing stop SCALED to the day-1 range
  + a hard stop. No blind 20-min buy, no opening-print "chase" gate (that gate was inert anyway).

Phase 2 — post-lockup long entry (the real edge: +16.8% median / 66% win in the last-12mo backtest):
  Resolve the REAL lock-up date from the SEC prospectus (src/ipo/lockup — lock-ups are 90/120/180d,
  NOT uniform), record the pre-lockup price, and on the post-lockup forced-supply dip HAND THE NAME
  OFF to the compounder universe, which sizes & accumulates it with its full NLV-scaled / capped / DCA
  logic. Confirmed dates auto-hand-off; estimates only alert. Phase 2 places NO IBKR orders.

No local event loop here — the jobs hand in the right connection (Phase 1 = options, Phase 2 = portfolio),
so the trader inherits it. (The old homegrown asyncio.new_event_loop() footgun is gone.)
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from ib_insync import IB, Stock, MarketOrder, Order

from src.core.database import get_db
from src.core.logger import get_logger
from src.ipo.models import IpoWatchlist
from src.portfolio.models import PortfolioHolding, PortfolioTransaction

log = get_logger(__name__)

# NOTE: no local event-loop management here. The IPO jobs (src/scheduler/jobs.py) already establish
# the correct loop for the connection they hand in — Phase 1 on the options conn, Phase 2 on the
# portfolio conn — so the trader inherits it. A homegrown asyncio.new_event_loop() here was the
# classic "standalone loop silently times out" footgun; it's gone.


# ── Phase 1 (opening-day "slack" flip) tunables ─────────────────────────────────────────────
# Backtest (102 liquid US IPOs, last 12mo): the day-1 CLOSE-vs-OPEN tell separates runners from
# faders (closed-strong +1.0% mean vs closed-weak −4.5%); the opening *premium vs offer* is useless.
# So we no longer buy blind 20min in — we wait for the first daily CLOSE, only flip names that closed
# strong, and trail with a stop scaled to the name's own day-1 range (a fixed 8% trail gets shaken out
# by the noise of a 30-90%-range debut). Phase 1 is a small scalp; the real edge is Phase 2 (post-lockup).
FLIP_MIN_CLOSE_ABOVE_OPEN_PCT = 3.0   # day-1 close must be >= this % above day-1 open to flip
FLIP_TRAIL_MIN_PCT = 10.0             # vol-scaled trailing-stop floor
FLIP_TRAIL_MAX_PCT = 25.0             # …and cap
FLIP_MAX_ENTRY_DAY = 4                # only enter within the first N trading days of listing (no chasing)


def flip_decision(d_open: float, d_high: float, d_low: float, d_close: float) -> dict:
    """Pure Phase-1 decision from the day-1 OHLC: should we flip, and at what trailing-stop %.

    Enter only if the first session CLOSED at least FLIP_MIN_CLOSE_ABOVE_OPEN_PCT above its OPEN
    (day-1 supply absorbed → buyers control day 2). Trailing stop is scaled to the day-1 range so a
    violently volatile debut isn't stopped out by its own noise. Returns {enter, trail_pct, reason}."""
    if not d_open or d_open <= 0:
        return {"enter": False, "trail_pct": FLIP_TRAIL_MAX_PCT, "reason": "no day-1 open"}
    close_above = (d_close - d_open) / d_open * 100.0
    rng = (d_high - d_low) / d_open * 100.0 if d_high and d_low else FLIP_TRAIL_MIN_PCT
    trail = max(FLIP_TRAIL_MIN_PCT, min(FLIP_TRAIL_MAX_PCT, rng))
    enter = close_above >= FLIP_MIN_CLOSE_ABOVE_OPEN_PCT
    return {"enter": enter, "trail_pct": round(trail, 1),
            "reason": f"close {close_above:+.1f}% vs open, day1 range {rng:.0f}%"}


class IpoTrader:
    """Manages IPO scanning, day-one flips, and lockup re-entries.

    IB SERIALIZATION CONTRACT: every public method here drives the shared ib_insync
    event loop directly (qualifyContracts / reqMktData / placeOrder / ib.sleep). The
    caller MUST hold the single shared IB RLock (get_ib_lock(), == get_portfolio_lock())
    for the whole call — otherwise it races concurrent scans/CC/put fetches and raises
    "This event loop is already running". See scheduler.jobs._job_ipo_* for the pattern.
    """

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
                if days_until > 1:
                    continue  # too early to scan
            except ValueError:
                continue  # invalid date format

            try:
                listing = self._get_listing(ipo.expected_ticker, ipo.exchange, ipo.currency)
                # days_listed counts completed daily bars: 1 = debut session still open → wait for the
                # CLOSE before judging. Only flip within the first few sessions (no chasing a week-old IPO).
                if not listing or listing["days_listed"] < 2:
                    continue
                if listing["days_listed"] > FLIP_MAX_ENTRY_DAY:
                    self._skip_flip(ipo, f"listed >{FLIP_MAX_ENTRY_DAY} sessions ago — no chase")
                    continue
                d1 = listing["day1"]
                decision = flip_decision(d1["open"], d1["high"], d1["low"], d1["close"])
                if not decision["enter"]:
                    self._skip_flip(ipo, f"day-1 closed weak ({decision['reason']})")
                    continue
                log.info("ipo_flip_armed", ticker=ipo.expected_ticker,
                         company=ipo.company_name, reason=decision["reason"])
                self._execute_day_two_buy(ipo, d1, decision)
            except Exception as e:
                log.debug("ipo_scan_check", ticker=ipo.expected_ticker, error=str(e))

    def _skip_flip(self, ipo: IpoWatchlist, reason: str):
        """The day-1 close didn't qualify (weak, or too late) — skip the Phase-1 scalp and route the
        name straight to Phase 2 (the real edge is post-lockup anyway)."""
        log.info("ipo_flip_skipped", ticker=ipo.expected_ticker, reason=reason)
        with get_db() as db:
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.status = "lockup_waiting" if entry.lockup_enabled else "flip_done"
                entry.updated_at = datetime.utcnow()

    def _get_listing(self, ticker: str, exchange: str, currency: str) -> Optional[dict]:
        """Listing state for a freshly-public ticker, or None if not yet tradeable.

        Returns {days_listed, day1:{open,high,low,close,date}, last}. days_listed is the count of
        completed daily bars (1 = debut session still in progress)."""
        contract = Stock(ticker, exchange, currency)
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            return None
        try:
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="", durationStr="10 D",
                barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
                formatDate=1, timeout=8,
            )
        except Exception:
            return None
        if not bars:
            return None
        d1 = bars[0]
        last = None
        try:
            self.ib.reqMarketDataType(1)   # live data only, not delayed
            td = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(2)
            last = td.last
            self.ib.cancelMktData(contract)
        except Exception:
            pass
        if not last or math.isnan(last) or last <= 0:
            last = d1.close
        return {
            "days_listed": len(bars),
            "day1": {"open": d1.open, "high": d1.high, "low": d1.low,
                     "close": d1.close, "date": str(d1.date)},
            "last": last,
        }

    def _execute_day_two_buy(self, ipo: IpoWatchlist, d1: dict, decision: dict):
        """Day-2 entry on a name that CLOSED strong on its debut (see flip_decision). Market buy, then
        a trailing stop SCALED to the day-1 range + a hard stop-loss. No opening-print 'chase' gate —
        the entry is gated on the day-1 close, not on intraday premium (that gate was inert anyway)."""
        from datetime import datetime as dt
        import pytz

        # ── Cooldown gate ──────────────────────────────────────────────
        et = pytz.timezone("America/New_York")
        now_et = dt.now(et)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_since_open = (now_et - market_open).total_seconds() / 60

        cooldown_minutes = 20  # minimum wait after open
        if minutes_since_open < cooldown_minutes:
            log.info("ipo_cooldown_waiting",
                     ticker=ipo.expected_ticker,
                     minutes_since_open=round(minutes_since_open, 1),
                     cooldown=cooldown_minutes)
            return  # scheduler will retry on next 30s scan

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
        trailing_order.trailingPercent = decision.get("trail_pct", ipo.flip_trailing_pct)
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
        """Phase 2 — the real edge: enter a LONG-TERM position after lock-up expiry (forced-supply dip),
        sized and accumulated by the COMPOUNDER (not a flat lockup_amount). Resolves the real lock-up
        date from the SEC prospectus, records the pre-lockup reference price, and on the post-lockup dip
        HANDS THE NAME OFF to the compounder universe. Places NO IBKR orders. Called daily."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with get_db() as db:
            names = db.query(IpoWatchlist).filter(
                IpoWatchlist.status.in_(["lockup_waiting", "flip_done"]),
                IpoWatchlist.lockup_enabled == True,
            ).all()
        for ipo in names:
            try:
                self._process_lockup(ipo, today)
            except Exception as e:
                log.error("ipo_lockup_check_error", ticker=ipo.expected_ticker, error=str(e))

    def _process_lockup(self, ipo: IpoWatchlist, today: str):
        """Resolve the real lock-up date, record the pre-lockup price, and hand the name to the
        compounder on the post-lockup dip. Auto only on a CONFIRMED date; alert on an estimate."""
        lockup, confidence = self._ensure_lockup_date(ipo, today)
        if not lockup:
            return

        from datetime import datetime as dt
        lockup_dt = dt.strptime(lockup, "%Y-%m-%d")
        # Record the pre-lockup reference ~2 weeks (≈10 trading days) before expiry (matches backtest).
        pre_start = (lockup_dt - timedelta(days=14)).strftime("%Y-%m-%d")
        if today >= pre_start and not ipo.pre_lockup_price:
            price = self._get_current_price(ipo.expected_ticker, ipo.exchange, ipo.currency)
            if price and price > 0:
                with get_db() as db:
                    entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
                    if entry:
                        entry.pre_lockup_price = price
                        entry.updated_at = datetime.utcnow()
                log.info("ipo_pre_lockup_price_recorded", ticker=ipo.expected_ticker, price=price)

        # After expiry: on the forced-supply dip, hand off to the compounder (confirmed dates only).
        if today >= lockup and ipo.pre_lockup_price:
            price = self._get_current_price(ipo.expected_ticker, ipo.exchange, ipo.currency)
            if not price or price <= 0:
                return
            dip_pct = ((ipo.pre_lockup_price - price) / ipo.pre_lockup_price) * 100
            if dip_pct < ipo.lockup_dip_pct:
                return
            log.info("ipo_lockup_dip_reached", ticker=ipo.expected_ticker,
                     pre_lockup=ipo.pre_lockup_price, current=price,
                     dip_pct=round(dip_pct, 1), confidence=confidence)
            if confidence == "confirmed":
                self._handoff_to_compounder(ipo, price, dip_pct)
            else:
                self._alert_lockup_estimate(ipo, price, dip_pct)

    def _ensure_lockup_date(self, ipo: IpoWatchlist, today: str) -> tuple[Optional[str], str]:
        """Return (lockup_date, confidence). Resolves the REAL lock-up period from the SEC prospectus
        when we don't already hold a confirmed one — lock-ups are 90/120/180d, not uniform (src/ipo/lockup)."""
        if ipo.lockup_date and (ipo.lockup_confidence or "") == "confirmed":
            return ipo.lockup_date, "confirmed"
        ipo_date = ipo.expected_date
        if not ipo_date:
            return ipo.lockup_date, (ipo.lockup_confidence or "low")
        try:
            from src.ipo.lockup import resolve_lockup
            res = resolve_lockup(ipo.expected_ticker, ipo_date)
        except Exception as e:
            log.warning("ipo_lockup_resolve_error", ticker=ipo.expected_ticker, error=str(e))
            res = None
        if res:
            with get_db() as db:
                entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
                if entry:
                    entry.lockup_date = res["end_date"]
                    entry.lockup_confidence = res["confidence"]
                    entry.lockup_source = res["source"]
                    entry.updated_at = datetime.utcnow()
            return res["end_date"], res["confidence"]
        if ipo.lockup_date:
            return ipo.lockup_date, (ipo.lockup_confidence or "low")
        # Last resort: a flagged 180-day ESTIMATE (never auto-traded — only alerts).
        from datetime import datetime as dt
        try:
            est = (dt.strptime(ipo_date, "%Y-%m-%d") + timedelta(days=180)).strftime("%Y-%m-%d")
        except ValueError:
            return None, "none"
        with get_db() as db:
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.lockup_date = est
                entry.lockup_confidence = "low"
                entry.lockup_source = "estimate_180d"
                entry.updated_at = datetime.utcnow()
        return est, "low"

    def _handoff_to_compounder(self, ipo: IpoWatchlist, price: float, dip_pct: float):
        """Add the unlocked name to the COMPOUNDER universe so the compounder sizes & accumulates it with
        its own logic (NLV-scaled, conviction-weighted, capped, DCA'd, fail-closed funding) — not a flat
        lockup_amount. The IPO rider is only the timing gate. In suggestion_mode the compounder proposes
        the buy for approval (human-gated — can't auto-pollute the book). No IBKR order placed here."""
        from src.portfolio.models import PortfolioWatchlist
        sym = ipo.expected_ticker
        with get_db() as db:
            exists = db.query(PortfolioWatchlist).filter(PortfolioWatchlist.symbol == sym).first()
            if not exists:
                db.add(PortfolioWatchlist(
                    symbol=sym, name=ipo.company_name,
                    exchange=ipo.exchange or "SMART", currency=ipo.currency or "USD",
                    sector="", tier="growth", category="growth",
                    # Modest starting scores so the compounder gives it a real target; the monthly
                    # screen re-scores it properly from fundamentals on its next run.
                    growth_score=60.0, quality_score=55.0, forward_growth_score=55.0,
                    valuation_score=50.0, fundamentals_complete=False,
                    rationale=f"IPO post-lockup entry (sourced by IPO rider, dip {dip_pct:.0f}%)",
                    screened_at=datetime.utcnow(),
                ))
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.status = "lockup_done"
                entry.lockup_entry_price = price
                entry.lockup_entry_time = datetime.utcnow()
                entry.updated_at = datetime.utcnow()
        log.info("ipo_handoff_to_compounder", ticker=sym, price=price, dip_pct=round(dip_pct, 1))
        try:
            from src.core.alerts import get_alert_manager
            get_alert_manager().send(
                title=f"🎯 IPO post-lockup → Compounder: {sym}",
                body=(f"{ipo.company_name}\nLock-up expired; dipped {dip_pct:.0f}% from pre-lockup.\n"
                      f"Added to the compounder universe — it sizes & accumulates (suggestion-gated)."),
                priority="high", tags="ipo",
            )
        except Exception:
            pass

    def _alert_lockup_estimate(self, ipo: IpoWatchlist, price: float, dip_pct: float):
        """Lock-up date is only an ESTIMATE (couldn't confirm from the prospectus). Don't auto-trade a
        possibly-wrong unlock — alert the user to confirm the date manually."""
        log.info("ipo_lockup_estimate_dip", ticker=ipo.expected_ticker, dip_pct=round(dip_pct, 1))
        try:
            from src.core.alerts import get_alert_manager
            get_alert_manager().send(
                title=f"⚠️ IPO lock-up dip (UNCONFIRMED date): {ipo.expected_ticker}",
                body=(f"{ipo.company_name} dipped {dip_pct:.0f}% near an ESTIMATED lock-up date. "
                      f"Confirm the real lock-up date before entering."),
                priority="default", tags="ipo",
            )
        except Exception:
            pass

    def _place_lockup_trailing_buy(self, ipo: IpoWatchlist, current_price: float):
        """Place trailing stop buy order to catch the bounce after lockup dip."""
        contract = Stock(ipo.expected_ticker, ipo.exchange, ipo.currency)
        self.ib.qualifyContracts(contract)

        shares = int(ipo.lockup_amount / current_price)
        if shares < 1:
            log.warning("ipo_lockup_insufficient_funds", ticker=ipo.expected_ticker)
            return

        # Check buying power before placing order — use self.ib directly
        # so Phase 2 (portfolio account) reads from the correct connection,
        # not the singleton options account connection.
        required_amount = current_price * shares
        try:
            values = self.ib.accountValues()
            buying_power = 0.0
            for v in values:
                if v.tag == "BuyingPower" and v.currency in ("BASE", "USD"):
                    try:
                        buying_power = float(v.value)
                        break
                    except (ValueError, TypeError):
                        pass
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
