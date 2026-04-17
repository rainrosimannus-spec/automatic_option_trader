"""
SPY Tail Hedge — buys rolling far-OTM SPY puts as portfolio insurance.

Funded by a fraction of collected premiums. Caps max portfolio drawdown
in crash scenarios where multiple short puts get assigned simultaneously.

Logic:
1. On schedule, check if we have an active hedge position
2. If no hedge or hedge DTE < roll threshold: buy new hedge put
3. Budget: max 4% of total collected premiums spent on hedges
4. Target: 5% OTM SPY put, ~30 DTE
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ib_insync import Stock, Option as IBOption

from src.broker.connection import get_ib
from src.broker.market_data import get_stock_price, get_put_contracts
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, TradeType, OrderStatus
from src.core.logger import get_logger

log = get_logger(__name__)


class TailHedge:
    """Manages rolling SPY OTM put hedges."""

    def __init__(self):
        self.cfg = get_settings().strategy

    def check_and_maintain_hedge(self) -> Optional[str]:
        """
        Main entry point — check if hedge needs to be placed or rolled.
        Returns action taken: "bought", "rolled", "exists", or None.
        """
        if not self.cfg.hedge_enabled:
            return None

        # Check budget — don't spend more than X% of total collected premiums
        if not self._within_budget():
            log.info("hedge_budget_exhausted")
            return None

        # Check if we have an active hedge
        hedge_pos = self._get_active_hedge()

        if hedge_pos is None:
            # No hedge — buy one
            success = self._buy_hedge()
            return "bought" if success else None

        # Check if hedge needs rolling (DTE too low)
        if self._needs_roll(hedge_pos):
            success = self._roll_hedge(hedge_pos)
            return "rolled" if success else None

        return "exists"

    def _get_active_hedge(self) -> Optional[Position]:
        """Find the current active SPY hedge position."""
        with get_db() as db:
            return (
                db.query(Position)
                .filter(
                    Position.symbol == "SPY",
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "hedge_put",
                )
                .first()
            )

    def _within_budget(self) -> bool:
        """Check if hedge spending is within budget."""
        with get_db() as db:
            # Total premiums collected ever
            all_positions = db.query(Position).all()
            total_collected = sum(p.total_premium_collected for p in all_positions)

            # Total spent on hedges
            hedge_trades = (
                db.query(Trade)
                .filter(Trade.notes.contains("hedge"))
                .all()
            )
            total_hedge_cost = sum(t.premium * 100 * t.quantity for t in hedge_trades if t.trade_type == TradeType.BUY_PUT)

        if total_collected <= 0:
            # No premiums collected yet — allow first hedge
            return True

        budget = total_collected * self.cfg.hedge_budget_pct
        remaining = budget - total_hedge_cost

        log.debug(
            "hedge_budget",
            total_collected=round(total_collected, 2),
            budget=round(budget, 2),
            spent=round(total_hedge_cost, 2),
            remaining=round(remaining, 2),
        )

        return remaining > 0

    def _needs_roll(self, pos: Position) -> bool:
        """Check if the hedge put needs to be rolled (DTE too low)."""
        if not pos.expiry:
            return True

        today = datetime.now().date()
        try:
            exp_date = datetime.strptime(pos.expiry, "%Y%m%d").date()
        except ValueError:
            return True

        dte = (exp_date - today).days
        return dte <= self.cfg.hedge_roll_dte

    def _find_hedge_contract(self) -> Optional[tuple[IBOption, float]]:
        """
        Find the best SPY put for hedging.
        Target: ~5% OTM, ~30 DTE.
        Returns (contract, ask_price) or None.
        """
        ib = get_ib()
        spy_price = get_stock_price("SPY", "SMART", "USD")
        if not spy_price:
            log.warning("no_spy_price_for_hedge")
            return None

        target_strike = round(spy_price * (1 - self.cfg.hedge_otm_pct))

        # Find option chains
        contract = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(contract)
        chains = ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)

        if not chains:
            return None

        chain = None
        for c in chains:
            if c.exchange == "SMART":
                chain = c
                break
        if not chain:
            chain = chains[0]

        # Find expiry closest to target DTE
        today = datetime.now().date()
        best_exp = None
        best_dte_diff = 999

        for exp_str in chain.expirations:
            exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
            dte = (exp_date - today).days
            if dte < 14:  # don't buy too short
                continue
            diff = abs(dte - self.cfg.hedge_dte_target)
            if diff < best_dte_diff:
                best_dte_diff = diff
                best_exp = exp_str

        if not best_exp:
            return None

        # Find strike closest to target
        best_strike = min(chain.strikes, key=lambda s: abs(s - target_strike))

        opt = IBOption("SPY", best_exp, best_strike, "P", "SMART")
        qualified = ib.qualifyContracts(opt)
        if not qualified:
            return None

        ticker = ib.reqMktData(opt, "", False, False)
        ib.sleep(2)
        ib.cancelMktData(opt)

        # Live ask only -- no BS fallback
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
        if not ask:
            log.info("hedge_no_live_ask", strike=best_strike, expiry=best_exp)
            return None

        exp_date = datetime.strptime(best_exp, "%Y%m%d").date()
        actual_dte = (exp_date - today).days

        log.info(
            "hedge_candidate_found",
            strike=best_strike,
            expiry=best_exp,
            dte=actual_dte,
            ask=round(ask, 2),
            spy_price=round(spy_price, 2),
            otm_pct=f"{(spy_price - best_strike) / spy_price:.1%}",
        )

        return (opt, ask)

    def _buy_hedge(self) -> bool:
        """Buy a new SPY hedge put."""
        result = self._find_hedge_contract()
        if not result:
            log.warning("no_hedge_contract_found")
            return False

        contract, ask_price = result
        ib = get_ib()

        from ib_insync import LimitOrder
        order = LimitOrder("BUY", self.cfg.hedge_contracts, round(ask_price, 2))
        order.tif = "DAY"

        trade = ib.placeOrder(contract, order)
        ib.sleep(1)

        if not trade:
            return False

        # Record in database
        with get_db() as db:
            pos = Position(
                symbol="SPY",
                status=PositionStatus.OPEN,
                position_type="hedge_put",
                strike=contract.strike,
                expiry=contract.lastTradeDateOrContractMonth,
                entry_premium=ask_price,
                quantity=self.cfg.hedge_contracts,
                total_premium_collected=0,  # hedge costs money, doesn't collect
            )
            db.add(pos)
            db.flush()

            trade_record = Trade(
                position_id=pos.id,
                symbol="SPY",
                trade_type=TradeType.BUY_PUT,
                strike=contract.strike,
                expiry=contract.lastTradeDateOrContractMonth,
                premium=ask_price,
                quantity=self.cfg.hedge_contracts,
                fill_price=ask_price,
                order_id=trade.order.orderId,
                order_status=OrderStatus.SUBMITTED,
                notes="hedge — SPY tail protection",
            )
            db.add(trade_record)

        log.info(
            "hedge_bought",
            strike=contract.strike,
            expiry=contract.lastTradeDateOrContractMonth,
            cost=round(ask_price * 100 * self.cfg.hedge_contracts, 2),
        )
        return True

    def _roll_hedge(self, old_pos: Position) -> bool:
        """Close old hedge and buy a new one."""
        log.info("hedge_rolling", old_strike=old_pos.strike, old_expiry=old_pos.expiry)

        # Close old position (let it expire or sell if it has value)
        with get_db() as db:
            pos = db.query(Position).get(old_pos.id)
            if pos:
                pos.status = PositionStatus.CLOSED
                pos.closed_at = datetime.utcnow()

        # Buy new hedge
        return self._buy_hedge()
