"""
Account information — positions, P&L, buying power.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ib_insync import PortfolioItem

from src.broker.connection import get_ib, get_ib_lock
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
    # Solvency buffer (IBKR ExcessLiquidity / AvailableFunds). Unlike cash_balance,
    # excess_liquidity does NOT go negative just because cash converted to marginable
    # stock on assignment — it's the true "can I take more risk" number. Default 0.0
    # so the empty/error fallbacks below remain valid without listing every field.
    excess_liquidity: float = 0.0
    available_funds: float = 0.0
    # USD per 1 unit of the account's BASE currency (Rain 2026-11-11). The account is
    # EUR-base, every strike / lot / collateral figure the gates compare against is USD.
    # get_account_summary() fills this from the hourly IBKR ExchangeRate cache; a summary
    # built by hand (tests, fallbacks) keeps 1.0 so nothing shifts there.
    fx_usd_per_base: float = 1.0

    @property
    def net_liquidation_usd(self) -> float:
        return self.net_liquidation * self.fx_usd_per_base

    @property
    def buying_power_usd(self) -> float:
        return self.buying_power * self.fx_usd_per_base

    @property
    def maintenance_margin_usd(self) -> float:
        return self.maintenance_margin * self.fx_usd_per_base

    @property
    def excess_liquidity_usd(self) -> float:
        return self.excess_liquidity * self.fx_usd_per_base


def usd_per_base() -> float:
    """USD per 1 BASE-currency unit from the hourly IBKR ExchangeRate cache (LOCAL→BASE,
    base = 1.0, so USD-per-base = 1 / rate[USD]). 1.0 when the cache is missing (USD base
    or no data) — logged once so a silent EUR≈USD never hides."""
    try:
        from src.portfolio.fx import load_fx_rates
        r = float(load_fx_rates().get("USD") or 0.0)
        if r > 0:
            return 1.0 / r
    except Exception as e:
        log.warning("usd_per_base_unavailable", error=str(e))
    return 1.0


def nlv_usd(summary) -> float:
    """NLV in USD from any summary-like object (falls back to the raw figure)."""
    if not summary:
        return 0.0
    v = getattr(summary, "net_liquidation_usd", None)
    return float(v) if v is not None else float(getattr(summary, "net_liquidation", 0.0) or 0.0)


def get_account_summary() -> AccountSummary:
    """Fetch key account values using cached account data."""
    ib = get_ib()
    try:
        # Use accountValues() — this returns data that IBKR pushes automatically
        # on connection, so it doesn't require a separate request/response cycle.
        # Much more reliable from scheduler threads than accountSummary().
        with get_ib_lock():
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
        excess_liquidity=_val("ExcessLiquidity"),
        available_funds=_val("AvailableFunds"),
        fx_usd_per_base=usd_per_base(),
    )


def get_portfolio_positions() -> list[PortfolioItem]:
    """Get current portfolio positions from IBKR for the options account only."""
    ib = get_ib()
    with get_ib_lock():
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
