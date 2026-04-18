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
                # Check if expiry has passed
                if put_pos.expiry and put_pos.expiry <= today:
                    symbol = put_pos.symbol
                    shares = stock_positions.get(symbol, 0)

                    if shares >= 100 * put_pos.quantity:
                        # Assignment detected — IBKR confirms stock appeared
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
        """Process a put assignment — close put position, open stock position."""
        log.info(
            "put_assigned",
            symbol=symbol,
            strike=put_pos.strike,
            premium=put_pos.entry_premium,
        )

        # Close the put position
        put_pos.status = PositionStatus.ASSIGNED
        put_pos.closed_at = datetime.utcnow()

        # Record assignment trade
        trade = Trade(
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
        )
        db.add(trade)

        # Create stock position (cost basis = strike - premium received)
        cost_basis = (put_pos.strike or 0) - put_pos.entry_premium
        from src.core.config import get_settings as _gs
        exit_mode_enabled = _gs().risk.wheel_exit_mode_enabled
        stock_pos = Position(
            symbol=symbol,
            status=PositionStatus.OPEN,
            position_type="stock",
            cost_basis=cost_basis,
            quantity=100 * put_pos.quantity,
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

    def write_covered_calls(self) -> list[str]:
        """
        For all stock positions from assignments, write covered calls.
        Returns list of symbols where calls were written.
        """
        if not self.cfg.wheel_enabled:
            return []

        log.info("scanning_for_covered_calls")
        written: list[str] = []

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
                    result = self._write_call(db, stock_pos, contracts=lots_to_cover)
                    if result:
                        written.append(symbol)
                except Exception as e:
                    log.error("covered_call_error", symbol=symbol, error=str(e))

        log.info("covered_calls_written", symbols=written)
        return written

    def _write_call(self, db, stock_pos: Position, contracts: int = 0) -> bool:
        """
        Screen and sell a covered call on an assigned stock position.
        Smart strike management:
        - Always sell above cost basis
        - If stock has recovered significantly, use lower delta (protect upside)
        - Progressive: as stock price rises above cost basis, widen the gap
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

        # Exit mode: bump delta range, add interest surcharge to min_strike
        from src.core.config import get_settings as _gs
        risk_cfg = _gs().risk
        interest_surcharge = 0.0
        if stock_pos.wheel_exit_mode:
            # Days held since assignment (opened_at is assignment time for wheel positions)
            try:
                from datetime import datetime as _dt
                days_held = (_dt.utcnow() - stock_pos.opened_at).days
            except Exception:
                days_held = 0
            interest_surcharge = max(days_held, 0) * (risk_cfg.wheel_exit_margin_rate_annual / 365.0) * (cost_basis or 0)
            cc_delta_min = risk_cfg.wheel_exit_delta_min
            cc_delta_max = risk_cfg.wheel_exit_delta_max
        else:
            # Wheel covered calls: goal is to get called away and return to cash
            # Use fixed delta range regardless of market regime — sell close to money
            # above cost basis, maximize fill probability and premium collection
            cc_delta_min, cc_delta_max = 0.30, 0.45

        # min_strike = net basis plus interest surcharge (exit mode) or net basis alone (normal)
        min_strike_value = net_cost_basis + interest_surcharge
        min_strike = min_strike_value if self.cfg.cc_above_cost_basis and min_strike_value > 0 else None

        log.info("covered_call_params", symbol=symbol,
                 exit_mode=stock_pos.wheel_exit_mode,
                 cost_basis=round(cost_basis, 2) if cost_basis else None,
                 net_cost_basis=round(net_cost_basis, 2),
                 interest_surcharge=round(interest_surcharge, 4),
                 min_strike=round(min_strike, 2) if min_strike else None,
                 current_price=round(current_price, 2) if current_price else None,
                 delta_range=(cc_delta_min, cc_delta_max))
        # Screen for the best call with adjusted parameters
        candidate = screen_calls(
            symbol,
            exchange=exchange,
            currency=currency,
            min_strike=min_strike,
            delta_min_override=cc_delta_min,
            delta_max_override=cc_delta_max,
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

                    if shares < 100:
                        # Stock was called away
                        self._handle_called_away(db, call_pos, symbol)
                        called.append(symbol)
                    else:
                        # Call expired worthless, keep stock
                        call_pos.status = PositionStatus.EXPIRED
                        call_pos.closed_at = datetime.utcnow()
                        call_pos.realized_pnl = call_pos.total_premium_collected

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
            # Calculate total P&L for the wheel cycle
            sale_proceeds = (call_pos.strike or 0) * stock_pos.quantity
            cost = (stock_pos.cost_basis or 0) * stock_pos.quantity
            total_premium = stock_pos.total_premium_collected
            realized = sale_proceeds - cost + total_premium

            stock_pos.status = PositionStatus.CLOSED
            stock_pos.closed_at = datetime.utcnow()
            stock_pos.realized_pnl = realized

            log.info(
                "wheel_cycle_complete",
                symbol=symbol,
                realized_pnl=round(realized, 2),
                total_premium=round(total_premium, 2),
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
