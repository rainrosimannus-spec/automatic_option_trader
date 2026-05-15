"""
Cash Bridge v2 — performance-based capital harvest from options to portfolio account.

Triggers a sweep when options account NLV reaches (benchmark x factor),
defaults to 2.0x (i.e. doubling). Sweep amount is (NLV x transfer_pct),
defaults to 10% of NLV. After sweep, benchmark resets to post-sweep NLV.

Capital injections increase the benchmark by exactly the injection amount,
so deposits never trigger fake sweeps and growth is measured only against
capital actually at work.

State is the source of truth — all settings live in PortfolioState rows
written by the Controls page (src/web/routes/controls.py). BridgeConfig
provides defaults when state is empty.

Two events update bridge_benchmark:
  1. Sweep: benchmark = post_sweep_NLV
  2. Injection: benchmark += injection_amount (via bump_bridge_benchmark hook
     called from src/portfolio/capital_injections.py)

Transfer execution path is unchanged from v1: IBKR Client Portal Gateway API
for automated transfer, with manual-execution fallback if the gateway is
unavailable.

Merged-mode safety: even if enabled=True, Bridge skips the sweep when
source_account == target_account (defensive belt-and-suspenders for the
case where someone enables Bridge before re-split happens).
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Optional

from ib_insync import IB

from src.core.logger import get_logger
from src.core.database import get_db
from src.portfolio.models import PortfolioState, PortfolioTransaction

log = get_logger(__name__)


class BridgeConfig:
    """Default configuration. Overridden by PortfolioState keys at runtime."""

    def __init__(
        self,
        enabled: bool = False,
        factor: float = 2.0,
        transfer_pct: float = 0.10,
        cooldown_days: int = 0,
        source_account: str = "",
        target_account: str = "",
        currency: str = "USD",
        dry_run: bool = True,
    ):
        self.enabled = enabled
        self.factor = factor
        self.transfer_pct = transfer_pct
        self.cooldown_days = cooldown_days
        self.source_account = source_account
        self.target_account = target_account
        self.currency = currency
        self.dry_run = dry_run


class CashBridge:
    """
    Daily check for benchmark-doubling trigger. On trigger, sweep configured
    percentage of NLV from options account to portfolio account.

    State keys (read at every check_and_transfer call):
      Settings:
        bridge_enabled, bridge_factor, bridge_transfer_pct,
        bridge_cooldown_days, bridge_dry_run
      Tracking:
        bridge_benchmark, bridge_last_check_date, bridge_last_check_nlv,
        bridge_last_sweep_date, bridge_last_transfer_amount,
        bridge_last_check_result, bridge_pending_transfer
    """

    def __init__(self, ib: IB, cfg: BridgeConfig):
        self.ib = ib
        self.cfg = cfg

    def check_and_transfer(self) -> Optional[dict]:
        """
        Daily check. Returns transfer details dict if executed, None otherwise.
        Reads runtime settings from PortfolioState; falls back to cfg defaults.
        """
        enabled = self._read_bool("bridge_enabled", self.cfg.enabled)
        if not enabled:
            return None

        if not self.cfg.source_account or not self.cfg.target_account:
            log.warning("bridge_accounts_not_configured")
            return None

        if self.cfg.source_account == self.cfg.target_account:
            log.info("bridge_skipped_merged_mode",
                     account=self.cfg.source_account,
                     message="source == target, refusing self-transfer")
            return None

        factor = self._read_float("bridge_factor", self.cfg.factor)
        transfer_pct = self._read_float("bridge_transfer_pct", self.cfg.transfer_pct * 100) / 100.0
        cooldown_days = self._read_int("bridge_cooldown_days", self.cfg.cooldown_days)
        dry_run = self._read_bool("bridge_dry_run", self.cfg.dry_run)

        today = date.today()

        if cooldown_days > 0:
            last_sweep = self._read_date("bridge_last_sweep_date")
            if last_sweep and (today - last_sweep).days < cooldown_days:
                log.debug("bridge_cooldown_active",
                          last_sweep=last_sweep.isoformat(),
                          cooldown_days=cooldown_days)
                return None

        nlv = self._get_account_net_liquidation(self.cfg.source_account)
        if nlv is None:
            log.warning("bridge_cannot_get_account_value",
                        account=self.cfg.source_account)
            return None

        benchmark = self._get_benchmark()

        trigger_level = benchmark * factor
        log.info("bridge_check",
                 account=self.cfg.source_account,
                 nlv=round(nlv, 2),
                 benchmark=round(benchmark, 2),
                 factor=factor,
                 trigger_level=round(trigger_level, 2),
                 ratio=round(nlv / benchmark, 3) if benchmark > 0 else None)

        self._store_state("bridge_last_check_date", datetime.utcnow().isoformat())
        self._store_state("bridge_last_check_nlv", str(round(nlv, 2)))

        if nlv < trigger_level:
            self._store_state("bridge_last_check_result", "below_threshold")
            return None

        transfer_amount = round(nlv * transfer_pct, 2)

        log.info("bridge_trigger_hit",
                 source=self.cfg.source_account,
                 target=self.cfg.target_account,
                 nlv=round(nlv, 2),
                 benchmark=round(benchmark, 2),
                 factor=factor,
                 transfer_pct=transfer_pct,
                 transfer_amount=transfer_amount,
                 currency=self.cfg.currency)

        if dry_run:
            log.info("bridge_dry_run_would_transfer",
                     amount=transfer_amount,
                     currency=self.cfg.currency,
                     source=self.cfg.source_account,
                     target=self.cfg.target_account)
            self._store_state("bridge_last_check_result", "dry_run")
            return {
                "status": "dry_run",
                "amount": transfer_amount,
                "currency": self.cfg.currency,
                "source": self.cfg.source_account,
                "target": self.cfg.target_account,
                "nlv": nlv,
                "benchmark": benchmark,
            }

        success = self._execute_transfer(transfer_amount)

        if success:
            post_sweep_nlv = nlv - transfer_amount
            self._store_state("bridge_benchmark", str(round(post_sweep_nlv, 2)))
            self._store_state("bridge_last_sweep_date", today.isoformat())
            self._store_state("bridge_last_transfer_amount", str(round(transfer_amount, 2)))
            self._store_state("bridge_last_check_result", "swept")
            self._record_transaction(transfer_amount, nlv, benchmark)

            log.info("bridge_transfer_completed",
                     amount=transfer_amount,
                     currency=self.cfg.currency,
                     new_benchmark=round(post_sweep_nlv, 2))

            return {
                "status": "completed",
                "amount": transfer_amount,
                "currency": self.cfg.currency,
                "source": self.cfg.source_account,
                "target": self.cfg.target_account,
                "nlv": nlv,
                "benchmark": benchmark,
                "new_benchmark": post_sweep_nlv,
            }
        else:
            self._store_state("bridge_last_check_result", "transfer_failed")
            return {
                "status": "failed",
                "amount": transfer_amount,
            }

    def _get_benchmark(self) -> float:
        """
        Read current benchmark from state. If unset, initialize from
        total_invested for the source account.
        """
        existing = self._read_float("bridge_benchmark", -1.0)
        if existing >= 0:
            return existing

        try:
            from src.portfolio.capital_injections import get_total_invested_usd
            initial = get_total_invested_usd(account_id=self.cfg.source_account)
            if initial > 0:
                self._store_state("bridge_benchmark", str(round(initial, 2)))
                log.info("bridge_benchmark_initialized",
                         account=self.cfg.source_account,
                         benchmark=round(initial, 2))
                return initial
        except Exception as e:
            log.warning("bridge_benchmark_init_error", error=str(e))

        return 0.0

    def _get_account_net_liquidation(self, account: str) -> Optional[float]:
        """Get net liquidation value for the specified account."""
        try:
            from src.portfolio.connection import get_portfolio_lock
            _ensure_event_loop()
            with get_portfolio_lock():
                values = self.ib.accountValues(account=account)
            for item in values:
                if (item.tag == "NetLiquidation" and
                        item.currency in (self.cfg.currency, "BASE")):
                    return float(item.value)
            for item in values:
                if item.tag == "NetLiquidation":
                    return float(item.value)
            return None
        except Exception as e:
            log.warning("bridge_account_query_error",
                        account=account, error=str(e))
            return None

    def _execute_transfer(self, amount: float) -> bool:
        """
        Execute internal IBKR account transfer via Client Portal Gateway API.
        Falls back to logging a pending-transfer flag for manual execution.
        """
        try:
            success = self._cp_gateway_transfer(amount)
            if success:
                return True

            log.warning("bridge_auto_transfer_unavailable",
                        amount=amount,
                        currency=self.cfg.currency,
                        source=self.cfg.source_account,
                        target=self.cfg.target_account,
                        message="Execute manually in IBKR Account Management")

            self._store_state(
                "bridge_pending_transfer",
                f"{amount},{self.cfg.currency},{self.cfg.source_account},"
                f"{self.cfg.target_account},{datetime.utcnow().isoformat()}"
            )
            return False

        except Exception as e:
            log.error("bridge_transfer_error", error=str(e))
            return False

    def _cp_gateway_transfer(self, amount: float) -> bool:
        """Transfer funds via IBKR Client Portal Gateway API at localhost:5000."""
        try:
            import urllib.request
            import json
            import ssl

            gateway_url = "https://localhost:5000/v1/api"
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

    def _record_transaction(self, amount: float, nlv: float, benchmark: float):
        """Record bridge transfer as a portfolio transaction row."""
        with get_db() as db:
            db.add(PortfolioTransaction(
                symbol="BRIDGE",
                action="bridge_transfer",
                shares=0,
                price=0,
                amount=amount,
                currency=self.cfg.currency,
                signal="benchmark_doubling",
                tier="bridge",
                notes=(
                    f"Bridge sweep: NLV {self.cfg.currency} {nlv:,.2f} reached "
                    f"benchmark {self.cfg.currency} {benchmark:,.2f} x factor. "
                    f"Transferred {self.cfg.currency} {amount:,.2f} "
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

    def _read_str(self, key: str, default: str) -> str:
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state and state.value is not None:
                return state.value
        return default

    def _read_bool(self, key: str, default: bool) -> bool:
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state and state.value is not None:
                return state.value.lower() == "true"
        return default

    def _read_float(self, key: str, default: float) -> float:
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state and state.value is not None:
                try:
                    return float(state.value)
                except (TypeError, ValueError):
                    return default
        return default

    def _read_int(self, key: str, default: int) -> int:
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state and state.value is not None:
                try:
                    return int(state.value)
                except (TypeError, ValueError):
                    return default
        return default

    def _read_date(self, key: str) -> Optional[date]:
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state and state.value is not None:
                try:
                    return date.fromisoformat(state.value)
                except (TypeError, ValueError):
                    return None
        return None


def bump_bridge_benchmark(injection_amount: float, account_id: str):
    """
    Hook called from src/portfolio/capital_injections.py when a new injection
    is synced. If the injection is for the configured bridge source_account,
    add the amount to bridge_benchmark so that the doubling rule measures
    growth on capital actually at work, not on deposits.
    """
    try:
        from src.core.config import get_settings
        cfg = get_settings()
        source_account = getattr(cfg.ibkr, "account", "")
        if not source_account or account_id != source_account:
            return

        with get_db() as db:
            state = db.query(PortfolioState).filter(
                PortfolioState.key == "bridge_benchmark"
            ).first()
            current = float(state.value) if state and state.value else 0.0
            new_benchmark = current + injection_amount

            if state:
                state.value = str(round(new_benchmark, 2))
                state.updated_at = datetime.utcnow()
            else:
                db.add(PortfolioState(
                    key="bridge_benchmark",
                    value=str(round(new_benchmark, 2)),
                ))

        log.info("bridge_benchmark_bumped",
                 account=account_id,
                 injection=round(injection_amount, 2),
                 old_benchmark=round(current, 2),
                 new_benchmark=round(new_benchmark, 2))
    except Exception as e:
        log.warning("bridge_benchmark_bump_error", error=str(e))


# _ensure_event_loop imported from connection.py — do not define locally.
# Local versions create a new loop which causes CancelledError on all IBKR calls.
from src.portfolio.connection import _ensure_event_loop
