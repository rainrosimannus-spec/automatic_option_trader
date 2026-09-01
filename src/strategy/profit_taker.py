"""
Profit taker — close positions at 55% profit and optionally roll into new entries.
Rolling = close profitable put → immediately scan for a new put on the same stock.
This extracts more premium from the same capital over time.
"""
from __future__ import annotations

from datetime import datetime
import pytz

from src.broker.market_data import get_stock_price
from src.broker.orders import buy_to_close_put
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, TradeType, OrderStatus
from src.core.logger import get_logger
from src.strategy.universe import UniverseManager

from ib_insync import Option as IBOption

log = get_logger(__name__)


def deep_itm_early_close_triggered(spot: float | None, strike: float, call_ask: float,
                                   dte: int, *, over_pct: float, max_extrinsic_pct: float,
                                   min_dte: int) -> bool:
    """Rule A gate: should a deep-ITM covered call be bought-to-close early?

    True only when all hold: the stock is genuinely deep ITM (spot ≥ strike*(1+over)),
    the call's remaining time value has decayed to ~0 (ask − intrinsic ≤ max_ext*strike
    — the only value we forfeit by closing now), and it's not about to self-resolve
    (dte ≥ min_dte). Pure/side-effect-free so it can be unit-tested and mirrors the
    MarsWalk engine's early-close pass exactly. Fails closed on a missing/zero spot.
    """
    if not spot or spot <= 0 or strike <= 0 or dte < min_dte:
        return False
    if spot < strike * (1.0 + over_pct):
        return False
    extrinsic = call_ask - max(0.0, spot - strike)
    return extrinsic <= max_extrinsic_pct * strike


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
            log.info("profit_check_no_live_price", symbol=pos.symbol,
                     reason="skipping -- no live ask, no BS fallback")
            return False
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

        IMPORTANT: in suggestion/auto-execute mode this MUST NOT redeploy via the
        live path. `_process_symbol` calls `sell_put` inline (put_seller.py), which
        bypasses the suggestion→queue→executor route — the single, budget-gated
        chokepoint. Two paths placing puts (this roll + the queue executor) is how
        the same contract got filled twice. In auto mode we no-op here and let the
        next scan redeploy the freed symbols through the one gated path.
        """
        from src.core.config import get_settings
        if get_settings().app.suggestion_mode:
            log.info("roll_skipped_suggestion_mode", symbols=symbols,
                     msg="auto mode: freed symbols redeploy via the next scan's "
                         "queued+budget-gated path, not the live roll")
            return

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

    # ── #2 Roll tested ~0DTE puts down-and-out (DEFAULT OFF) ──────────
    def _get_roll_count(self, db, key: str) -> int:
        from src.core.models import SystemState
        row = db.query(SystemState).filter(SystemState.key == key).first()
        try:
            return int(row.value) if row and row.value else 0
        except Exception:
            return 0

    def _set_roll_count(self, db, key: str, value: int) -> None:
        from src.core.models import SystemState
        row = db.query(SystemState).filter(SystemState.key == key).first()
        if row:
            row.value = str(value)
        else:
            db.add(SystemState(key=key, value=str(value)))

    def check_tested_puts(self) -> list[str]:
        """#2 Roll tested ~0DTE short puts down-and-out instead of taking
        assignment. DEFAULT OFF (strategy.roll_tested_puts_enabled) — it
        deliberately overrides the "let DTE<=3 expire/assign" behaviour, so it
        only acts when explicitly enabled.

        For each short put within roll_tested_dte_max of expiry that is ITM
        (underlying < strike - itm_buffer), under the per-symbol daily roll cap,
        and where a down-and-out replacement would yield net credit >=
        roll_tested_min_credit: buy the tested put to close (avoid assignment,
        back to cash) and route a fresh entry through the normal scan (which
        respects suggestion mode). Returns symbols acted on.
        """
        if not self.cfg.roll_tested_puts_enabled:
            return []

        from datetime import datetime as _dt
        from src.broker.market_data import get_option_live_price
        from src.strategy.screener import screen_puts

        log.info("tested_put_roll_check_started")
        acted: list[str] = []

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
                    if not pos.strike or not pos.expiry:
                        continue
                    stock = self.universe.get_stock(pos.symbol)
                    currency = stock.currency if stock else "USD"
                    exchange = stock.exchange if stock else "SMART"
                    opt_exchange = stock.opt_exchange if stock else "SMART"

                    # Only act while the market is open
                    if _minutes_to_market_open(currency) > 0:
                        continue

                    # DTE gate — only the near-expiry tail
                    try:
                        exp_date = _dt.strptime(pos.expiry, "%Y%m%d").date()
                        dte = (exp_date - _dt.now().date()).days
                    except Exception:
                        continue
                    if dte < 0 or dte > self.cfg.roll_tested_dte_max:
                        continue

                    # Tested (ITM) gate
                    price = get_stock_price(pos.symbol, exchange, currency)
                    if not price or price >= (pos.strike - self.cfg.roll_tested_itm_buffer):
                        continue

                    # Per-symbol daily roll cap
                    today_str = _dt.now().strftime("%Y-%m-%d")
                    state_key = f"roll_count:{pos.symbol}:{today_str}"
                    rolls_today = self._get_roll_count(db, state_key)
                    if rolls_today >= self.cfg.roll_tested_max_per_day:
                        log.info("tested_put_roll_cap_reached", symbol=pos.symbol,
                                 rolls_today=rolls_today)
                        continue

                    # Buyback cost (live ask of the tested put)
                    _, buyback_ask = get_option_live_price(
                        pos.symbol, pos.expiry, pos.strike, "P", opt_exchange, currency
                    )
                    if not buyback_ask or buyback_ask <= 0:
                        log.info("tested_put_roll_no_buyback_quote", symbol=pos.symbol)
                        continue

                    # Down-and-out replacement: screen a put a bit further out.
                    # Net-credit gate uses its bid vs the buyback ask.
                    replacement = screen_puts(
                        pos.symbol, exchange=opt_exchange, currency=currency,
                        stock_exchange=exchange,
                        dte_min=dte + 1, dte_max=dte + 8,
                    )
                    if not replacement:
                        log.info("tested_put_roll_no_replacement", symbol=pos.symbol)
                        continue

                    net_credit = replacement.bid - buyback_ask
                    if net_credit < self.cfg.roll_tested_min_credit:
                        log.info("tested_put_roll_below_min_credit", symbol=pos.symbol,
                                 net_credit=round(net_credit, 3),
                                 min_credit=self.cfg.roll_tested_min_credit)
                        continue

                    # Buy to close the tested put → avoid assignment, return to cash
                    trade = buy_to_close_put(
                        symbol=pos.symbol,
                        expiry=pos.expiry,
                        strike=pos.strike,
                        quantity=pos.quantity,
                        limit_price=round(buyback_ask, 2),
                        exchange=opt_exchange,
                        currency=currency,
                    )
                    if not trade:
                        continue

                    db.add(Trade(
                        position_id=pos.id,
                        symbol=pos.symbol,
                        trade_type=TradeType.BUY_PUT,
                        strike=pos.strike,
                        expiry=pos.expiry,
                        premium=buyback_ask,
                        quantity=pos.quantity,
                        fill_price=buyback_ask,
                        order_id=trade.order.orderId,
                        order_status=OrderStatus.SUBMITTED,
                        notes=(f"#2 roll: BTC tested put to avoid assignment "
                               f"(net credit est {net_credit:.2f} vs new ${replacement.strike}P)"),
                    ))
                    self._set_roll_count(db, state_key, rolls_today + 1)
                    db.commit()
                    log.info("tested_put_rolled_btc", symbol=pos.symbol,
                             strike=pos.strike, buyback=round(buyback_ask, 2),
                             new_strike=replacement.strike,
                             net_credit=round(net_credit, 3))
                    acted.append(pos.symbol)
                except Exception as e:
                    log.error("tested_put_roll_error", symbol=pos.symbol, error=str(e))

        # Re-enter down-and-out via the normal scan (respects suggestion mode)
        if acted:
            self._roll_positions(acted)

        log.info("tested_put_roll_check_done", acted=acted)
        return acted

    def check_covered_calls(self) -> list[str]:
        """
        Monitor open covered calls on wheel-assigned stocks.

        Iron logic (Phase 16): if the CC is 80%+ profitable, close it.
        The wheel scheduler writes a fresh closer-to-money CC next cycle
        so the strike tracks the stock down, improving exit probability.

        No ITM roll-up — assignment IS the goal for wheel-assigned stocks.
        Skip DTE <= 3 — let expire worthless, not worth the fees.

        Returns list of symbols acted on.
        """
        log.info("cc_profit_check_started")
        acted: list[str] = []

        with get_db() as db:
            open_calls = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "covered_call",
                )
                .all()
            )

            for pos in open_calls:
                try:
                    stock = self.universe.get_stock(pos.symbol)
                    currency = stock.currency if stock else "USD"
                    mins_to_open = _minutes_to_market_open(currency)
                    if mins_to_open > 60:
                        log.debug("cc_check_skipped_market_closed",
                                  symbol=pos.symbol, mins_to_open=round(mins_to_open))
                        continue

                    # Parse DTE
                    try:
                        from datetime import datetime as _dt
                        exp_date = _dt.strptime(pos.expiry, "%Y%m%d").date()
                        dte = (exp_date - _dt.now().date()).days
                    except Exception:
                        dte = 99

                    if dte <= 0:
                        continue

                    exchange = stock.exchange if stock else "SMART"
                    opt_exchange = stock.opt_exchange if stock else "SMART"

                    # Get live call price
                    from src.broker.market_data import get_option_live_price, get_stock_price, get_stock_live_price
                    live_bid, live_ask = get_option_live_price(
                        pos.symbol, pos.expiry, pos.strike, "C", opt_exchange, currency
                    )
                    if not live_ask or live_ask <= 0:
                        log.info("cc_check_no_live_price", symbol=pos.symbol,
                                 reason="skipping -- no live ask, no BS/bid/intrinsic fallback")
                        continue

                    entry_premium = pos.entry_premium
                    if not entry_premium or entry_premium <= 0:
                        continue

                    # ── Deep-ITM early-close (Rule A, 2026-08-05) ──
                    # When the stock has run far above the CC strike AND the call's
                    # remaining time value has decayed to ~0, the lot is capped but
                    # sits on margin until expiry. Buy-to-close the call now — SAFETY:
                    # only the buy-to-close happens here; the freed (uncovered) lot is
                    # sold next cycle by the wheel's _live_exit_opportunity, which
                    # reads IBKR positions fail-closed (afaa425) — so no naked-short
                    # window ever opens. Banks the capped gain early, stops the carry,
                    # frees capital for new puts. Default OFF (see RiskConfig).
                    rc = get_settings().risk
                    if getattr(rc, "cc_early_close_enabled", False):
                        spot = get_stock_live_price(pos.symbol, exchange, currency)
                        if deep_itm_early_close_triggered(
                                spot, pos.strike, live_ask, dte,
                                over_pct=rc.cc_early_close_stock_over_strike_pct,
                                max_extrinsic_pct=rc.cc_early_close_max_extrinsic_pct,
                                min_dte=rc.cc_early_close_min_dte):
                            log.info("cc_deep_itm_early_close",
                                     symbol=pos.symbol, dte=dte, strike=pos.strike,
                                     spot=round(spot, 2), call_ask=round(live_ask, 2),
                                     extrinsic=round(live_ask - max(0.0, spot - pos.strike), 2),
                                     over_strike_pct=f"{(spot / pos.strike - 1):.1%}")
                            success = self._close_covered_call(
                                db, pos, live_ask, opt_exchange, currency,
                                reason="deep_itm_early_close")
                            if success:
                                acted.append(pos.symbol)
                            continue  # acted/churn-blocked — skip the 80%-profit branch

                    # ── Iron-logic CC exit (Phase 16) ──
                    # Single rule: if CC is 80%+ profitable, close it. Next wheel cycle
                    # writes a fresh closer-to-money CC at lower strike, tracking the
                    # stock down to improve exit probability.
                    # Skip DTE <= 3 — let expire worthless, not worth the fees.
                    # No ITM roll-up — assignment IS the goal for wheel-assigned stocks.
                    # The _roll_call_up implementation was deleted with this rule (dead since
                    # 34099b6, 2026-04-18); `git show 34099b6^:src/strategy/profit_taker.py` has it.
                    if dte > 3:
                        profit_pct = (entry_premium - live_ask) / entry_premium
                        # wheel_cc_profit_threshold lives on RiskConfig, not StrategyConfig
                        # (self.cfg). Reading it off self.cfg raised AttributeError that the
                        # broad except below swallowed as cc_check_error — so this 80%-profit
                        # CC auto-close never actually ran. It was masked by the unlocked
                        # get_option_live_price race ("no live ask" continued out above before
                        # ever reaching here); fixed in the same change.
                        threshold = get_settings().risk.wheel_cc_profit_threshold
                        if profit_pct >= threshold:
                            log.info("cc_profit_target_hit",
                                     symbol=pos.symbol, dte=dte,
                                     threshold_pct=f"{threshold:.0%}",
                                     entry=round(entry_premium, 2),
                                     current=round(live_ask, 2),
                                     profit_pct=f"{profit_pct:.0%}")
                            success = self._close_covered_call(db, pos, live_ask, opt_exchange, currency, reason="profit_take")
                            if success:
                                acted.append(pos.symbol)

                except Exception as e:
                    log.error("cc_check_error", symbol=pos.symbol, error=str(e))

        log.info("cc_profit_check_done", acted=acted)
        return acted

    def _close_covered_call(self, db, pos: Position, ask_price: float,
                          opt_exchange: str, currency: str, reason: str = "profit_take") -> bool:
        """Buy to close a covered call. Records trade, position stays open until trade_sync confirms fill.

        Order pricing: ask * 1.02 (floor ask + 0.05) to absorb stale-feed error.
        Churn prevention: skip if existing SUBMITTED order is within 10% of new target price.
        """
        from src.broker.orders import buy_to_close_call
        from src.core.models import Trade, TradeType, OrderStatus

        # Pad the price: buy slightly above ask to improve fill probability
        target_price = round(max(ask_price * 1.02, ask_price + 0.05), 2)

        existing = db.query(Trade).filter(
            Trade.position_id == pos.id,
            Trade.trade_type == TradeType.BUY_CALL,
            Trade.order_status == OrderStatus.SUBMITTED,
        ).all()

        if existing:
            for ex in existing:
                ex_price = ex.premium or 0
                if ex_price > 0 and abs(target_price - ex_price) / ex_price < 0.10:
                    log.info("cc_close_skip_replace_within_10pct",
                             symbol=pos.symbol,
                             existing_order_id=ex.order_id,
                             existing_price=round(ex_price, 2),
                             new_target=target_price)
                    return False

            try:
                from src.broker.orders import cancel_order
                from src.broker.connection import get_ib
                ib = get_ib()
                live_trades = ib.trades()
                for ex in existing:
                    if ex.order_id:
                        for lt in live_trades:
                            if lt.order.orderId == ex.order_id:
                                try:
                                    cancel_order(lt)
                                except Exception as e:
                                    log.warning("cancel_existing_cc_close_failed",
                                            symbol=pos.symbol, error=str(e))
                                break
                    ex.order_status = OrderStatus.CANCELLED
                db.commit()
            except Exception as e:
                log.warning("cancel_existing_cc_check_failed", symbol=pos.symbol, error=str(e))

        # Self-healing reprice: a prior close that was CANCELLED unfilled (e.g.
        # swept by an earlier market scan before the BUY-skip fix, or reconciled
        # away) does NOT block this retry — the existing-order query above filters
        # only SUBMITTED. Log it so the re-place is observable in the journal.
        prior_cancelled = db.query(Trade).filter(
            Trade.position_id == pos.id,
            Trade.trade_type == TradeType.BUY_CALL,
            Trade.order_status == OrderStatus.CANCELLED,
            Trade.ibkr_exec_id.is_(None),
        ).order_by(Trade.created_at.desc()).first()
        if prior_cancelled:
            log.info("cc_close_reattempt_after_cancel",
                     symbol=pos.symbol, prior_order_id=prior_cancelled.order_id,
                     new_target=target_price)

        trade = buy_to_close_call(
            symbol=pos.symbol,
            expiry=pos.expiry or "",
            strike=pos.strike or 0,
            quantity=pos.quantity,
            limit_price=target_price,
            exchange=opt_exchange,
            currency=currency,
        )
        if not trade:
            return False

        trade_record = Trade(
            position_id=pos.id,
            symbol=pos.symbol,
            trade_type=TradeType.BUY_CALL,
            strike=pos.strike or 0,
            expiry=pos.expiry or "",
            premium=target_price,
            quantity=pos.quantity,
            fill_price=target_price,
            order_id=trade.order.orderId,
            order_status=OrderStatus.SUBMITTED,
            notes=f"CC {reason} at {target_price:.2f} (ask was {ask_price:.2f})",
        )
        db.add(trade_record)
        db.commit()
        log.info("cc_close_order_placed", symbol=pos.symbol,
                 reason=reason, order_id=trade.order.orderId,
                 ask=round(ask_price, 2), limit_price=target_price)
        return True
