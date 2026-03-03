"""
Account information — positions, P&L, buying power.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ib_insync import PortfolioItem

from src.broker.connection import get_ib
from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class AccountSummary:
    net_liquidation: float
    buying_power: float
    cash_balance: float
    unrealized_pnl: float
    realized_pnl: float
    maintenance_margin: float


def get_account_summary() -> AccountSummary:
    """Fetch key account values using cached account data."""
    ib = get_ib()
    try:
        # Use accountValues() — this returns data that IBKR pushes automatically
        # on connection, so it doesn't require a separate request/response cycle.
        # Much more reliable from scheduler threads than accountSummary().
        values = ib.accountValues()
        
        if not values:
            # Trigger a refresh and wait
            ib.reqAccountUpdates(True, "")
            ib.sleep(2)
            values = ib.accountValues()

        if not values:
            log.warning("account_values_empty")
            return AccountSummary(
                net_liquidation=0, buying_power=0, cash_balance=0,
                unrealized_pnl=0, realized_pnl=0, maintenance_margin=0,
            )
    except Exception as e:
        log.warning("account_summary_error", error=str(e))
        return AccountSummary(
            net_liquidation=0, buying_power=0, cash_balance=0,
            unrealized_pnl=0, realized_pnl=0, maintenance_margin=0,
        )

    def _val(tag: str) -> float:
        """Get account value, preferring BASE currency for consistency."""
        # First try BASE (account's base currency — most reliable)
        for v in values:
            if v.tag == tag and v.currency == "BASE":
                try:
                    return float(v.value)
                except (ValueError, TypeError):
                    continue
        # Fallback to USD
        for v in values:
            if v.tag == tag and v.currency == "USD":
                try:
                    return float(v.value)
                except (ValueError, TypeError):
                    continue
        # Last resort: any currency
        for v in values:
            if v.tag == tag:
                try:
                    return float(v.value)
                except (ValueError, TypeError):
                    continue
        return 0.0

    return AccountSummary(
        net_liquidation=_val("NetLiquidation"),
        buying_power=_val("BuyingPower"),
        cash_balance=_val("CashBalance"),
        unrealized_pnl=_val("UnrealizedPnL"),
        realized_pnl=_val("RealizedPnL"),
        maintenance_margin=_val("MaintMarginReq"),
    )


def get_portfolio_positions() -> list[PortfolioItem]:
    """Get current portfolio positions from IBKR for the options account only."""
    ib = get_ib()
    positions = ib.portfolio()
    # Filter to options account only (TWS returns positions from all accounts)
    try:
        from src.core.config import get_settings
        account_id = get_settings().ibkr.account
        if account_id:
            positions = [p for p in positions if p.account == account_id]
    except Exception:
        pass
    return positions


def get_stock_positions() -> dict[str, int]:
    """
    Get stock positions as {symbol: shares}.
    Used to detect assignments (new stock positions from put exercise).
    """
    positions = get_portfolio_positions()
    stocks = {}
    for pos in positions:
        if pos.contract.secType == "STK" and pos.position != 0:
            stocks[pos.contract.symbol] = int(pos.position)
    return stocks


def get_option_positions() -> list[PortfolioItem]:
    """Get only option positions."""
    positions = get_portfolio_positions()
    return [p for p in positions if p.contract.secType == "OPT"]


def get_buying_power() -> float:
    """Get current available buying power."""
    summary = get_account_summary()
    return summary.buying_power
