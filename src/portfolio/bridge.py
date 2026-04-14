"""
Cash Bridge — annual harvest from options account to portfolio account.

Flow:
  1. July 31st (configurable): check options account net liquidation
  2. If value > min_portfolio_value (default 1M EUR): calculate transfer amount
  3. Transfer configurable % (default 10%) via IBKR internal account transfer
  4. Portfolio buyer detects new cash on next scan and deploys it

Transfer method: IBKR internal account transfer between linked accounts.
This requires both accounts to be linked under the same IBKR master account.

Note: IBKR Client Portal API (or FlexQuery) is used for the transfer,
not the TWS API (which doesn't support fund transfers).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, date
from typing import Optional

from ib_insync import IB

from src.core.logger import get_logger
from src.core.database import get_db
from src.portfolio.models import PortfolioState, PortfolioTransaction

log = get_logger(__name__)


class BridgeConfig:
    """Configuration for the Cash Bridge."""
    def __init__(
        self,
        enabled: bool = False,
        min_portfolio_value: float = 1_000_000.0,  # EUR
        transfer_pct: float = 0.10,                  # 10%
        transfer_month: int = 7,                      # July
        transfer_day: int = 31,                       # 31st
        source_account: str = "",                     # options IBKR account ID
        target_account: str = "",                     # portfolio IBKR account ID
        currency: str = "EUR",
        dry_run: bool = True,                         # safety: log but don't transfer
    ):
        self.enabled = enabled
        self.min_portfolio_value = min_portfolio_value
        self.transfer_pct = transfer_pct
        self.transfer_month = transfer_month
        self.transfer_day = transfer_day
        self.source_account = source_account
        self.target_account = target_account
        self.currency = currency
        self.dry_run = dry_run


class CashBridge:
    """
    Annual cash transfer from options account to portfolio account.
    
    Checks once daily. On the configured date (default July 31),
    if the options account exceeds the threshold, transfers the
    configured percentage to the portfolio account.
    """

    def __init__(self, ib: IB, cfg: BridgeConfig):
        self.ib = ib
        self.cfg = cfg

    def check_and_transfer(self) -> Optional[dict]:
        """
        Main entry point — called daily by scheduler.
        Returns transfer details dict if executed, None otherwise.
        """
        if not self.cfg.enabled:
            return None

        if not self.cfg.source_account or not self.cfg.target_account:
            log.warning("bridge_accounts_not_configured")
            return None

        today = date.today()

        # Only execute on the configured date
        if today.month != self.cfg.transfer_month or today.day != self.cfg.transfer_day:
            return None

        # Check if already executed this year
        if self._already_executed_this_year(today.year):
            log.info("bridge_already_executed_this_year", year=today.year)
            return None

        # Get options account value
        net_liq = self._get_account_net_liquidation(self.cfg.source_account)
        if net_liq is None:
            log.warning("bridge_cannot_get_account_value",
                        account=self.cfg.source_account)
            return None

        log.info("bridge_check",
                 account=self.cfg.source_account,
                 net_liquidation=round(net_liq, 2),
                 threshold=self.cfg.min_portfolio_value)

        # Check threshold
        if net_liq < self.cfg.min_portfolio_value:
            log.info("bridge_below_threshold",
                     net_liq=round(net_liq, 2),
                     threshold=self.cfg.min_portfolio_value)
            self._record_check(today.year, net_liq, transferred=False,
                               reason="below_threshold")
            return None

        # Calculate transfer amount
        transfer_amount = round(net_liq * self.cfg.transfer_pct, 2)

        log.info("bridge_transfer_calculated",
                 source=self.cfg.source_account,
                 target=self.cfg.target_account,
                 net_liq=round(net_liq, 2),
                 transfer_pct=self.cfg.transfer_pct,
                 transfer_amount=transfer_amount,
                 currency=self.cfg.currency)

        # Execute transfer
        if self.cfg.dry_run:
            log.info("bridge_dry_run_would_transfer",
                     amount=transfer_amount,
                     currency=self.cfg.currency,
                     source=self.cfg.source_account,
                     target=self.cfg.target_account)
            self._record_check(today.year, net_liq, transferred=False,
                               reason="dry_run", amount=transfer_amount)
            return {
                "status": "dry_run",
                "amount": transfer_amount,
                "currency": self.cfg.currency,
                "source": self.cfg.source_account,
                "target": self.cfg.target_account,
                "net_liq": net_liq,
            }

        success = self._execute_transfer(transfer_amount)

        if success:
            self._record_check(today.year, net_liq, transferred=True,
                               amount=transfer_amount)
            self._record_transaction(transfer_amount, net_liq)

            log.info("bridge_transfer_completed",
                     amount=transfer_amount,
                     currency=self.cfg.currency)

            return {
                "status": "completed",
                "amount": transfer_amount,
                "currency": self.cfg.currency,
                "source": self.cfg.source_account,
                "target": self.cfg.target_account,
                "net_liq": net_liq,
            }
        else:
            self._record_check(today.year, net_liq, transferred=False,
                               reason="transfer_failed", amount=transfer_amount)
            return {
                "status": "failed",
                "amount": transfer_amount,
            }

    # ── Account value query ──────────────────────────────────
    def _get_account_net_liquidation(self, account: str) -> Optional[float]:
        """Get net liquidation value for a specific account."""
        try:
            _ensure_event_loop()
            values = self.ib.accountValues(account=account)
            for item in values:
                if (item.tag == "NetLiquidation" and
                        item.currency in (self.cfg.currency, "BASE")):
                    return float(item.value)
            # Fallback: any currency
            for item in values:
                if item.tag == "NetLiquidation":
                    return float(item.value)
            return None
        except Exception as e:
            log.warning("bridge_account_query_error",
                        account=account, error=str(e))
            return None

    # ── Transfer execution ───────────────────────────────────
    def _execute_transfer(self, amount: float) -> bool:
        """
        Execute internal IBKR account transfer.
        
        IBKR TWS API does not directly support fund transfers between accounts.
        Options:
        1. Use IBKR Client Portal API (Web API) — POST /iserver/account/transfer
        2. Use IBKR FlexQuery to verify, then manual/scheduled transfer
        3. Use DTS (Deposit/Transfer Service) via Client Portal web
        
        For automated operation, we use the Client Portal Gateway API.
        This requires the CP Gateway to be running (separate from TWS).
        
        If CP Gateway is not available, we log the transfer request
        and send a notification for manual execution.
        """
        try:
            # Attempt Client Portal Gateway API transfer
            success = self._cp_gateway_transfer(amount)
            if success:
                return True

            # Fallback: log for manual execution
            log.warning("bridge_auto_transfer_unavailable",
                        amount=amount,
                        currency=self.cfg.currency,
                        source=self.cfg.source_account,
                        target=self.cfg.target_account,
                        message="Please execute this transfer manually in IBKR Account Management")
            
            # Store pending transfer for dashboard notification
            self._store_state(
                "bridge_pending_transfer",
                f"{amount},{self.cfg.currency},{self.cfg.source_account},{self.cfg.target_account},{datetime.utcnow().isoformat()}"
            )
            return False

        except Exception as e:
            log.error("bridge_transfer_error", error=str(e))
            return False

    def _cp_gateway_transfer(self, amount: float) -> bool:
        """
        Transfer funds via IBKR Client Portal Gateway API.
        
        Requires CP Gateway running at localhost:5000 (default).
        Endpoint: POST /pa/transactions
        
        See: https://www.interactivebrokers.com/api/doc.html#tag/Account/paths/
        """
        try:
            import urllib.request
            import json

            gateway_url = "https://localhost:5000/v1/api"

            # Build transfer request
            payload = {
                "type": "INTERNAL",
                "accountId": self.cfg.source_account,
                "targetAccountId": self.cfg.target_account,
                "amount": amount,
                "currency": self.cfg.currency,
            }

            req = urllib.request.Request(
                f"{gateway_url}/pa/transactions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            # CP Gateway uses self-signed certs
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                result = json.loads(resp.read())
                log.info("bridge_cp_transfer_response", result=result)
                return resp.status == 200

        except Exception as e:
            log.debug("bridge_cp_gateway_not_available", error=str(e))
            return False

    # ── State management ─────────────────────────────────────
    def _already_executed_this_year(self, year: int) -> bool:
        """Check if bridge transfer already happened this year."""
        with get_db() as db:
            state = db.query(PortfolioState).filter(
                PortfolioState.key == f"bridge_last_transfer_year"
            ).first()
            if state and state.value == str(year):
                return True
            return False

    def _record_check(self, year: int, net_liq: float, transferred: bool,
                      reason: str = "", amount: float = 0):
        """Record bridge check in state."""
        self._store_state("bridge_last_check_date", datetime.utcnow().isoformat())
        self._store_state("bridge_last_check_net_liq", str(round(net_liq, 2)))

        if transferred:
            self._store_state("bridge_last_transfer_year", str(year))
            self._store_state("bridge_last_transfer_amount", str(round(amount, 2)))
            self._store_state("bridge_last_transfer_date", datetime.utcnow().isoformat())
        elif reason:
            self._store_state("bridge_last_check_result", reason)

    def _record_transaction(self, amount: float, net_liq: float):
        """Record bridge transfer as a portfolio transaction."""
        with get_db() as db:
            db.add(PortfolioTransaction(
                symbol="BRIDGE",
                action="bridge_transfer",
                shares=0,
                price=0,
                amount=amount,
                currency=self.cfg.currency,
                signal="annual_harvest",
                tier="bridge",
                notes=(
                    f"Annual bridge transfer: {self.cfg.transfer_pct*100:.0f}% of "
                    f"{self.cfg.currency} {net_liq:,.2f} = {self.cfg.currency} {amount:,.2f} "
                    f"from {self.cfg.source_account} to {self.cfg.target_account}"
                ),
            ))

    def _store_state(self, key: str, value: str):
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state:
                state.value = value
                state.updated_at = datetime.utcnow()
            else:
                db.add(PortfolioState(key=key, value=value))


# _ensure_event_loop imported from connection.py — do not define locally
# Local versions create a new loop which causes CancelledError on all IBKR calls
from src.portfolio.connection import _ensure_event_loop
