"""
Wheel strategy — detect put assignments and write covered calls.

Flow:
1. Detect expired/assigned puts (new stock position appears)
2. Update position records
3. Write covered calls on assigned stock
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.broker.account import get_stock_positions
from src.broker.orders import sell_covered_call
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import (
    Position, Trade, PositionStatus, TradeType, OrderStatus,
)
from src.core.logger import get_logger
from src.strategy.screener import screen_calls
from src.strategy.risk import RiskManager
from src.strategy.universe import UniverseManager

log = get_logger(__name__)


def _realized_cc_premium_per_share(db, stock_pos) -> float:
    """
    Sum realized_pnl from CLOSED covered call positions on this stock since
    assignment. Returns a per-share figure.

    Unlike total_premium_collected (optimistic, at-write turnover tracker),
    this reflects actual realized premium after buybacks/assignments/expiry.
    Used for net_cost_basis calculations that drive strike selection.
    """
    closed_ccs = (
        db.query(Position)
        .filter(
            Position.symbol == stock_pos.symbol,
            Position.position_type == "covered_call",
            Position.status.in_([
                PositionStatus.CLOSED,
                PositionStatus.ASSIGNED,
                PositionStatus.EXPIRED,
            ]),
            Position.opened_at >= stock_pos.opened_at,
        )
        .all()
    )
    cc_total = sum((p.realized_pnl or 0) for p in closed_ccs)
    shares = max(stock_pos.quantity, 1)
    return cc_total / shares


class WheelManager:
    """Manages the wheel: assignment detection → covered call writing."""

    def __init__(self, risk: RiskManager, universe: UniverseManager | None = None):
        self.risk = risk
        self.universe = universe or UniverseManager()
        self.cfg = get_settings().strategy

    def check_assignments(self) -> list[str]:
        """
        Detect put assignments by comparing IBKR stock positions
        against our tracked short puts that have expired.
        Returns list of newly assigned symbols.
        """
        log.info("checking_assignments")
        assigned_symbols: list[str] = []

        # Get current stock positions from broker
        stock_positions = get_stock_positions()

        with get_db() as db:
            # Find short puts that should have expired
            expired_puts = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "short_put",
                )
                .all()
            )

            today = datetime.now().strftime("%Y%m%d")

            for put_pos in expired_puts:
                # Only act once the expiry day has fully PASSED (strict <).
                # Comparing <= today fires at 00:00 ET on expiry day, before the
                # option has actually expired — premature. Matches trade_sync.
                if put_pos.expiry and put_pos.expiry < today:
                    symbol = put_pos.symbol

                    # IBKR is the source of truth. A worthless expiry is booked
                    # by IBKR as a buy-to-close (BUY_PUT, ~$0). If such a closing
                    # fill exists, the put was NOT assigned — let trade_sync mark
                    # it EXPIRED/CLOSED. Stock presence alone is NOT evidence of
                    # assignment: a covered call on the same symbol always has
                    # stock behind it, which previously caused worthless-expired
                    # puts to be misread as assigned.
                    closing_fill = (
                        db.query(Trade)
                        .filter(
                            Trade.symbol == symbol,
                            Trade.strike == put_pos.strike,
                            Trade.expiry == put_pos.expiry,
                            Trade.trade_type == TradeType.BUY_PUT,
                            Trade.order_status == OrderStatus.FILLED,
                            Trade.created_at >= put_pos.opened_at,
                        )
                        .first()
                    )
                    if closing_fill:
                        log.info("put_expiry_worthless_ibkr_confirmed",
                                 symbol=symbol, strike=put_pos.strike,
                                 expiry=put_pos.expiry)
                        continue

                    shares = stock_positions.get(symbol, 0)
                    if shares >= 100 * put_pos.quantity:
                        # No buy-to-close + shares delivered → genuine assignment.
                        self._handle_assignment(db, put_pos, symbol)
                        assigned_symbols.append(symbol)
                    else:
                        # No stock position — do NOT mark expired locally.
                        # trade_sync is the sole source of truth for worthless expiry.
                        # This avoids timezone flipping (e.g. AUD options expiring
                        # in Sydney time while server clock is behind).
                        log.debug("put_expiry_pending_ibkr_confirmation",
                                  symbol=symbol, expiry=put_pos.expiry)

        log.info("assignment_check_done", assigned=assigned_symbols)
        return assigned_symbols

    def _handle_assignment(self, db, put_pos: Position, symbol: str) -> None:
        """Process a put assignment — close put position, open stock position.

        Idempotency: if an OPEN stock Position already exists for this symbol
        with quantity matching the assignment (100 * put.quantity), skip the
        stock Position creation. This prevents duplicate stock rows when
        check_assignments runs multiple times against the same assigned put
        (e.g., race between concurrent scheduler runs, or re-fire after a
        previous call's commit was rolled back).

        The put-side state changes (status=ASSIGNED, trade row) remain
        idempotent: re-setting an ASSIGNED position to ASSIGNED is harmless,
        and the existing duplicate check on trade insertion handles the
        Trade row.
        """
        log.info(
            "put_assigned",
            symbol=symbol,
            strike=put_pos.strike,
            premium=put_pos.entry_premium,
        )

        # Close the put position
        put_pos.status = PositionStatus.ASSIGNED
        put_pos.closed_at = datetime.utcnow()

        # Record assignment trade — but only once. Previously this inserted a
        # fresh ASSIGNMENT row on every check_assignments cycle (the put kept
        # being reopened by trade_sync while still live in IBKR), producing
        # dozens of duplicates. Dedupe on (position_id, ASSIGNMENT).
        existing_assignment = (
            db.query(Trade)
            .filter(
                Trade.position_id == put_pos.id,
                Trade.trade_type == TradeType.ASSIGNMENT,
            )
            .first()
        )
        if not existing_assignment:
            db.add(Trade(
                position_id=put_pos.id,
                symbol=symbol,
                trade_type=TradeType.ASSIGNMENT,
                strike=put_pos.strike or 0,
                expiry=put_pos.expiry or "",
                premium=0,
                quantity=put_pos.quantity,
                fill_price=put_pos.strike or 0,
                order_status=OrderStatus.FILLED,
                notes="Put assigned — received 100 shares",
            ))

        # Idempotency guard: skip stock Position creation if one already exists
        # for this symbol with the expected quantity. Defensive against race
        # conditions where _handle_assignment is called more than once for the
        # same underlying assignment event.
        expected_qty = 100 * put_pos.quantity
        existing_stock = db.query(Position).filter(
            Position.symbol == symbol,
            Position.status == PositionStatus.OPEN,
            Position.position_type == "stock",
            Position.is_wheel == True,
        ).first()
        if existing_stock:
            log.info(
                "assignment_stock_position_already_exists",
                symbol=symbol,
                existing_id=existing_stock.id,
                existing_qty=existing_stock.quantity,
                expected_qty=expected_qty,
                note="skipping duplicate stock Position creation",
            )
            return

        # Create stock position (cost basis = strike - premium received)
        cost_basis = (put_pos.strike or 0) - put_pos.entry_premium
        from src.core.config import get_settings as _gs
        exit_mode_enabled = _gs().risk.wheel_exit_mode_enabled
        stock_pos = Position(
            symbol=symbol,
            status=PositionStatus.OPEN,
            position_type="stock",
            cost_basis=cost_basis,
            quantity=expected_qty,
            total_premium_collected=put_pos.total_premium_collected,
            is_wheel=True,
            wheel_exit_mode=exit_mode_enabled,
        )
        db.add(stock_pos)
        if exit_mode_enabled:
            log.info("wheel_exit_mode_activated", symbol=symbol,
                     cost_basis=round(cost_basis, 2))

    def _handle_expiry_worthless(self, db, put_pos: Position) -> None:
        """Put expired worthless — full premium is profit."""
        log.info(
            "put_expired_worthless",
            symbol=put_pos.symbol,
            strike=put_pos.strike,
            premium=put_pos.entry_premium,
        )
        put_pos.status = PositionStatus.EXPIRED
        put_pos.closed_at = datetime.utcnow()
        put_pos.realized_pnl = put_pos.total_premium_collected

    def check_pre_market_exit(self) -> list[str]:
        """
        Pre-market wheel-exit check.

        For each currently-uncovered wheel stock position, fetch a live
        quote and create a sell_stock suggestion if the mid-price is at
        or above (assignment_strike + sell_fee_per_share).

        Threshold uses the original ASSIGNMENT trade's strike — not the
        stored cost_basis (which is strike - premium). Reading 1: the rule
        fires at "called away" level. CC premium accumulated during the
        wheel is bonus, not part of the exit threshold.

        Quote validation: requires a two-sided quote with bid > 0,
        ask > 0, and (ask - bid) / mid < 2% to reject phantom oddlot
        pre-market quotes.

        Returns list of symbols where a suggestion was created.
        """
        if not self.cfg.wheel_enabled:
            return []

        from src.core.config import get_settings as _gs
        from src.core.suggestions import create_suggestion
        from src.broker.market_data import get_stock_live_quote
        from src.core.models import Trade, TradeType, OrderStatus

        sell_fee = _gs().risk.wheel_sell_fee_per_share
        fired: list[str] = []

        log.info("wheel_exit_scan_started")

        with get_db() as db:
            stock_positions = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "stock",
                    Position.is_wheel == True,
                )
                .all()
            )

            from src.broker.orders import get_cached_open_orders
            open_orders = get_cached_open_orders()

            symbols_seen = set()
            for stock_pos in stock_positions:
                symbol = stock_pos.symbol
                if symbol in symbols_seen:
                    continue
                symbols_seen.add(symbol)

                # Same uncovered-detection logic as write_covered_calls
                all_stock = db.query(Position).filter(
                    Position.symbol == symbol,
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "stock",
                    Position.is_wheel == True,
                ).all()
                total_shares = sum(p.quantity for p in all_stock)
                lots_needed = total_shares // 100

                open_calls = db.query(Position).filter(
                    Position.symbol == symbol,
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "covered_call",
                ).all()
                covered_contracts = sum(p.quantity for p in open_calls)

                from datetime import date as _date
                today_str = _date.today().strftime("%Y-%m-%d")
                filled_calls_today = db.query(Trade).filter(
                    Trade.symbol == symbol,
                    Trade.trade_type == TradeType.SELL_CALL,
                    Trade.order_status == OrderStatus.FILLED,
                    Trade.created_at >= today_str,
                ).count()
                covered_contracts += filled_calls_today

                pending_contracts = sum(
                    o.get("qty", 0) for o in open_orders
                    if o.get("symbol") == symbol and o.get("right") == "C"
                )
                from src.core.suggestions import TradeSuggestion
                pending_db = db.query(TradeSuggestion).filter(
                    TradeSuggestion.symbol == symbol,
                    TradeSuggestion.action == "sell_covered_call",
                    TradeSuggestion.status == "submitted",
                ).count()
                pending_contracts += pending_db

                lots_to_cover = lots_needed - covered_contracts - pending_contracts
                if lots_to_cover <= 0:
                    continue  # has CC coverage — wheel handles this

                # Recover assignment strike from the ASSIGNMENT trade
                assignment_trade = db.query(Trade).filter(
                    Trade.symbol == symbol,
                    Trade.trade_type == TradeType.ASSIGNMENT,
                ).order_by(Trade.created_at.desc()).first()

                if not assignment_trade or not assignment_trade.strike:
                    log.warning("wheel_exit_no_assignment_strike",
                                symbol=symbol,
                                note="cannot compute threshold without strike")
                    continue

                strike = assignment_trade.strike
                threshold = strike + sell_fee

                # Fetch live quote with bid/ask/last
                quote = get_stock_live_quote(symbol)
                if quote is None:
                    log.info("wheel_exit_no_quote", symbol=symbol)
                    continue

                bid, ask, last = quote
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid > 0 else 1.0

                if spread_pct >= 0.02:
                    log.info("wheel_exit_spread_too_wide",
                             symbol=symbol, bid=bid, ask=ask,
                             spread_pct=round(spread_pct, 4))
                    continue

                if mid < threshold:
                    log.info("wheel_exit_below_threshold",
                             symbol=symbol, mid=round(mid, 2),
                             threshold=round(threshold, 2),
                             strike=strike)
                    continue

                # Conditions met — create suggestion
                create_suggestion(
                    symbol=symbol,
                    action="sell_stock",
                    quantity=total_shares,
                    limit_price=bid,  # conservative — guarantees fill at bid or better
                    source="wheel_exit",
                    signal=f"mid={round(mid, 2)} strike={strike} fee={sell_fee}",
                    rationale=(
                        f"Pre-market exit opportunity: mid ${round(mid, 2)} "
                        f">= strike ${strike} + fee ${sell_fee}. "
                        f"Sell {total_shares} shares of {symbol} to exit wheel "
                        f"at or above called-away level."
                    ),
                    current_price=mid,
                    order_type="LMT",
                    funding_source="wheel",
                )
                log.info("wheel_exit_suggestion_fired",
                         symbol=symbol, mid=round(mid, 2),
                         strike=strike, shares=total_shares)
                fired.append(symbol)

        log.info("wheel_exit_scan_completed", symbols=fired)
        return fired

    def write_covered_calls(self) -> list[str]:
        """
        For all stock positions from assignments, write covered calls.
        Returns list of symbols where calls were written.
        """
        if not self.cfg.wheel_enabled:
            return []

        log.info("scanning_for_covered_calls")
        written: list[str] = []

        # Regime switch for CC selection — evaluate the crash detector ONCE per
        # scan (day-level idempotent state machine) and thread it into _write_call.
        # crash_active=True → bolster branch; False → velocity-always branch.
        try:
            crash_active, _crash_reason = self.risk.evaluate_crash_detector()
        except Exception as e:
            log.warning("cc_crash_detector_error", error=str(e))
            crash_active = False

        with get_db() as db:
            stock_positions = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "stock",
                    Position.is_wheel == True,
                )
                .all()
            )

            from src.broker.orders import get_cached_open_orders
            open_orders = get_cached_open_orders()

            # Group stock positions by symbol to handle multiple lots
            symbols_seen = set()
            for stock_pos in stock_positions:
                symbol = stock_pos.symbol
                if symbol in symbols_seen:
                    continue
                symbols_seen.add(symbol)

                # Count total stock shares for this symbol
                all_stock = db.query(Position).filter(
                    Position.symbol == symbol,
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "stock",
                    Position.is_wheel == True,
                ).all()
                total_shares = sum(p.quantity for p in all_stock)
                lots_needed = total_shares // 100

                # Count open covered call contracts
                open_calls = db.query(Position).filter(
                    Position.symbol == symbol,
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "covered_call",
                ).all()
                covered_contracts = sum(p.quantity for p in open_calls)

                # Also count filled call trades today — catches fills before trade_sync runs
                from src.core.models import Trade, TradeType, OrderStatus
                from datetime import date as _date
                today_str = _date.today().strftime("%Y-%m-%d")
                filled_calls_today = db.query(Trade).filter(
                    Trade.symbol == symbol,
                    Trade.trade_type == TradeType.SELL_CALL,
                    Trade.order_status == OrderStatus.FILLED,
                    Trade.created_at >= today_str,
                ).count()
                covered_contracts += filled_calls_today

                # Count pending IBKR call orders
                pending_contracts = sum(
                    o.get("qty", 0) for o in open_orders
                    if o.get("symbol") == symbol and o.get("right") == "C"
                )
                # Also check DB for submitted CC suggestions (survives restart)
                from src.core.suggestions import TradeSuggestion
                pending_db = db.query(TradeSuggestion).filter(
                    TradeSuggestion.symbol == symbol,
                    TradeSuggestion.action == "sell_covered_call",
                    TradeSuggestion.status == "submitted",
                ).count()
                pending_contracts += pending_db

                lots_to_cover = lots_needed - covered_contracts - pending_contracts

                if lots_to_cover <= 0:
                    log.info("covered_call_fully_covered",
                             symbol=symbol, lots=lots_needed,
                             covered=covered_contracts, pending=pending_contracts)
                    continue

                log.info("covered_call_lots_to_cover",
                         symbol=symbol, lots_needed=lots_needed,
                         covered=covered_contracts, pending=pending_contracts,
                         to_cover=lots_to_cover)

                try:
                    result = self._write_call(db, stock_pos, contracts=lots_to_cover, crash_active=crash_active)
                    if result:
                        written.append(symbol)
                except Exception as e:
                    log.error("covered_call_error", symbol=symbol, error=str(e))

        log.info("covered_calls_written", symbols=written)
        return written

    def _write_call(self, db, stock_pos: Position, contracts: int = 0, crash_active: bool = False) -> bool:
        """
        Screen and sell a covered call on an assigned stock position.

        Regime-specific (2026-06-23), switched by the crash detector:
        - Normal (crash_active=False) → VELOCITY-ALWAYS: deep-ITM exit-velocity
          call on every assigned lot + breakeven floor relaxed by
          cc_exit_loss_tolerance_pct → called away in days, small loss accepted.
        - Crash (crash_active=True) → BOLSTER: no exit-velocity, strict net-basis
          floor, defensive patient OTM CCs out to cc_crash_dte_max.
        Strike floor never below (relaxed) net cost basis; rescue band still
        applies for names already below breakeven.
        """
        symbol = stock_pos.symbol
        cost_basis = stock_pos.cost_basis
        exchange = self.universe.get_exchange(symbol)
        currency = self.universe.get_currency(symbol)
        contract_size = self.universe.get_contract_size(symbol)

        # Get current stock price to determine recovery level
        from src.broker.market_data import get_stock_price
        current_price = get_stock_price(symbol, exchange, currency)

        # Net cost basis = true breakeven including realized CC premiums.
        # Uses realized_pnl from closed CCs (honest) rather than total_premium_collected
        # (optimistic turnover figure) so loss-on-buyback is reflected.
        realized_cc_per_share = _realized_cc_premium_per_share(db, stock_pos)
        net_cost_basis = (cost_basis or 0) - realized_cc_per_share

        # ── Regime-specific CC selection (2026-06-23) ──────────────────────
        # DEFAULT = VELOCITY-ALWAYS in every regime: deep-ITM exit-velocity call on
        # EVERY assigned lot + breakeven floor relaxed by cc_exit_loss_tolerance_pct
        # → called away in days, small loss accepted, capital recycled to the put
        # engine. Replaces the old below-MA200 distressed-exit + interest-surcharge
        # branch (velocity-always now covers broken names too).
        #
        # The crash-bolster branch (defensive patient OTM + strict floor when the
        # crash detector fires) is RETAINED but DISABLED by default
        # (cc_crash_bolster_enabled=False). MarsWalk A/B (2026-06-23,
        # data/cc_regime_sweep_ab) REJECTED every bolster variant — defensive AND
        # aggressive-dump — across all 7 crash regimes (negative CRASH-sum vs
        # velocity-everywhere, max-DD unchanged): the CC branch isn't a crash lever
        # because crash P&L is dominated by the held-stock notional, and holding
        # longer forgoes the recycling velocity captures. Crash defense lives on the
        # put-entry side (crash detector → strangle/halt) + hedge module. Flag kept
        # for future experimentation. Mirrored in MarsWalk engine + dashboard chip.
        from src.core.config import get_settings as _gs
        risk_cfg = _gs().risk

        in_rescue = bool(current_price and cost_basis and current_price < cost_basis * risk_cfg.cc_rescue_threshold)
        bolster = crash_active and getattr(risk_cfg, "cc_crash_bolster_enabled", False)
        dte_max_override = None

        if bolster:
            # BOLSTER (off by default): strict floor, defensive patient OTM, longer DTE.
            wheel_branch = "crash_bolster"
            cc_delta_min = risk_cfg.cc_crash_delta_min
            cc_delta_max = risk_cfg.cc_crash_delta_max
            dte_max_override = risk_cfg.cc_crash_dte_max
            floor_basis = net_cost_basis                       # strict: never below breakeven
        else:
            # VELOCITY-ALWAYS: relax floor by tolerance for a fast small-loss exit;
            # deep-ITM exit-velocity is attempted below for all non-rescue names.
            floor_basis = net_cost_basis * (1.0 - max(risk_cfg.cc_exit_loss_tolerance_pct, 0.0))
            if in_rescue:
                # Stock already 5%+ below basis — deep-ITM won't clear the floor;
                # widen to find any far-OTM candidate above the (relaxed) floor.
                wheel_branch = "rescue"
                cc_delta_min, cc_delta_max = 0.05, 0.35
            else:
                wheel_branch = "velocity"
                cc_delta_min = self.cfg.cc_delta_min
                cc_delta_max = self.cfg.cc_delta_max

        min_strike = floor_basis if self.cfg.cc_above_cost_basis and floor_basis > 0 else None

        log.info("covered_call_params", symbol=symbol,
                 exit_mode=stock_pos.wheel_exit_mode,
                 wheel_branch=wheel_branch,
                 crash_active=crash_active,
                 cost_basis=round(cost_basis, 2) if cost_basis else None,
                 net_cost_basis=round(net_cost_basis, 2),
                 min_strike=round(min_strike, 2) if min_strike else None,
                 current_price=round(current_price, 2) if current_price else None,
                 dte_max=dte_max_override if dte_max_override is not None else self.cfg.cc_dte_max,
                 delta_range=(cc_delta_min, cc_delta_max))

        candidate = None
        # Velocity-always: try a deep-ITM call (near-certain call-away next
        # expiry) on EVERY assigned lot → return to cash in days. Skipped in
        # rescue (stock already below breakeven, no deep-ITM strike clears the
        # floor) and when the (default-off) bolster branch is active.
        if (self.cfg.wheel_exit_velocity_enabled and risk_cfg.cc_velocity_always
                and not bolster and not in_rescue):
            candidate = screen_calls(
                symbol,
                exchange=exchange,
                currency=currency,
                min_strike=min_strike,
                delta_min_override=self.cfg.wheel_exit_velocity_delta_min,
                delta_max_override=self.cfg.wheel_exit_velocity_delta_max,
            )
            if candidate:
                log.info("cc_exit_velocity_deep_itm", symbol=symbol,
                         strike=candidate.strike, delta=round(candidate.delta, 2),
                         note="deep-ITM CC for fast call-away (velocity-always)")

        # Fallback band: crash bolster (patient OTM, longer DTE), rescue (far-OTM),
        # or normal patient band when no deep-ITM candidate cleared the floor.
        if not candidate:
            candidate = screen_calls(
                symbol,
                exchange=exchange,
                currency=currency,
                min_strike=min_strike,
                delta_min_override=cc_delta_min,
                delta_max_override=cc_delta_max,
                max_dte_override=dte_max_override,
            )

        if not candidate:
            log.debug("no_call_candidate", symbol=symbol)
            return False

        # Place the order
        if not contracts:
            contracts = stock_pos.quantity // contract_size
        trade = sell_covered_call(
            symbol=symbol,
            expiry=candidate.expiry,
            strike=candidate.strike,
            quantity=contracts,
            limit_price=round(candidate.bid, 2),
            exchange=exchange,
            currency=currency,
        )

        if not trade:
            return False

        # Record trade only — Position will be created by trade_sync after fill
        trade_record = Trade(
            position_id=None,
            symbol=symbol,
            trade_type=TradeType.SELL_CALL,
            strike=candidate.strike,
            expiry=candidate.expiry,
            premium=candidate.bid,
            quantity=contracts,
            fill_price=candidate.bid,
            order_id=trade.order.orderId,
            order_status=OrderStatus.SUBMITTED,
            delta_at_entry=candidate.delta,
            iv_at_entry=candidate.iv,
            # Decision-time quote (same unit as premium/fill_price here) for
            # Consigliere execution-quality. Guarded: never raises if missing.
            bid_at_entry=getattr(candidate, "bid", None),
            ask_at_entry=getattr(candidate, "ask", None),
            mid_at_entry=getattr(candidate, "mid", None),
        )
        db.add(trade_record)

        # Create a TradeSuggestion entry so the covered call appears on the Suggestions page
        from src.core.suggestions import TradeSuggestion
        from datetime import timedelta
        suggestion = TradeSuggestion(
            symbol=symbol,
            action="sell_covered_call",
            order_type="sell_covered_call",
            quantity=contracts,
            limit_price=round(candidate.bid, 2),
            strike=candidate.strike,
            expiry=candidate.expiry,
            right="C",
            source="options",
            tier="wheel",
            signal=f"delta={round(candidate.delta, 3)} wheel",
            rationale=f"Wheel: sell covered call {candidate.expiry} ${candidate.strike}C @ ${round(candidate.bid, 2)} (delta {round(candidate.delta, 3)}, IV {round(candidate.iv * 100, 1)}%)",
            current_price=round(current_price, 2) if current_price else None,
            iv_rank=round(candidate.iv * 100, 1),
            est_cost=round(candidate.bid * contract_size * contracts, 2),
            status="submitted",
            reviewed_at=None,
            review_note="Pending fill — submitted to IBKR",
            rank=1,
            rank_score=1.0,
            funding_source="wheel",
            opt_exchange=exchange,
            opt_currency=currency,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db.add(suggestion)

        # Update stock position's total premium
        stock_pos.total_premium_collected += candidate.bid * contract_size * contracts

        log.info(
            "covered_call_sold",
            symbol=symbol,
            strike=candidate.strike,
            expiry=candidate.expiry,
            premium=round(candidate.bid, 2),
            cost_basis=round(cost_basis, 2) if cost_basis else None,
            current_price=round(current_price, 2) if current_price else None,
        )
        return True

    def check_called_away(self) -> list[str]:
        """
        Detect covered calls that were assigned (stock called away).
        Closes both the call and stock positions.
        """
        called: list[str] = []
        stock_positions = get_stock_positions()

        with get_db() as db:
            open_calls = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "covered_call",
                )
                .all()
            )

            today = datetime.now().strftime("%Y%m%d")

            for call_pos in open_calls:
                if call_pos.expiry and call_pos.expiry <= today:
                    symbol = call_pos.symbol
                    shares = stock_positions.get(symbol, 0)

                    if shares < 100 * call_pos.quantity:
                        # Stock was called away (early exercise OR post-expiry assignment).
                        # IBKR confirms via shares dropping below covered amount.
                        self._handle_called_away(db, call_pos, symbol)
                        called.append(symbol)
                    else:
                        # Stock still held — call has NOT been exercised.
                        # Do NOT mark expired locally; trade_sync is the sole
                        # source of truth for worthless expiry. This avoids
                        # flipping status repeatedly while IBKR still has the
                        # contract open (especially intra-day on expiry date).
                        log.debug("call_expiry_pending_ibkr_confirmation",
                                  symbol=symbol, strike=call_pos.strike, expiry=call_pos.expiry)

        return called

    def _handle_called_away(self, db, call_pos: Position, symbol: str) -> None:
        """Process covered call assignment — stock sold at strike."""
        log.info("stock_called_away", symbol=symbol, strike=call_pos.strike)

        # Close the call position
        call_pos.status = PositionStatus.ASSIGNED
        call_pos.closed_at = datetime.utcnow()

        # Close the stock position
        stock_pos = (
            db.query(Position)
            .filter(
                Position.symbol == symbol,
                Position.status == PositionStatus.OPEN,
                Position.position_type == "stock",
            )
            .first()
        )
        if stock_pos:
            # Mark stock position CLOSED. Do NOT compute realized_pnl here —
            # trade_sync owns that calculation from BUY_STOCK/SELL_STOCK trades.
            # The previous formula (sale - cost + total_premium) double-counted
            # the put premium because cost_basis is already net of put premium,
            # AND total_premium_collected was already realized when each option
            # closed. Result was wildly negative realized values on assignments.
            stock_pos.status = PositionStatus.CLOSED
            stock_pos.closed_at = datetime.utcnow()
            log.info(
                "wheel_cycle_complete",
                symbol=symbol,
                total_premium=round(stock_pos.total_premium_collected, 2),
                note="realized_pnl computed by trade_sync from stock trades",
            )

        # Record the trade
        trade = Trade(
            position_id=call_pos.id,
            symbol=symbol,
            trade_type=TradeType.CALLED_AWAY,
            strike=call_pos.strike or 0,
            expiry=call_pos.expiry or "",
            premium=0,
            quantity=call_pos.quantity,
            fill_price=call_pos.strike or 0,
            order_status=OrderStatus.FILLED,
            notes="Covered call assigned — stock called away",
        )
        db.add(trade)
