"""
Portfolio IBKR connection — maintains its own connection to a separate account.
"""
from __future__ import annotations

import asyncio
import socket
import time
from typing import Optional

from ib_insync import IB

from src.core.logger import get_logger

log = get_logger(__name__)

_portfolio_ib: Optional[IB] = None


def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP check — is TWS/Gateway listening on this port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def get_portfolio_ib(host: str, port: int, client_id: int, account: str = "",
                     readonly: bool = True) -> IB:
    """Return the portfolio IB connection, connecting if necessary.
    
    Does a quick TCP port check first — if Portfolio TWS isn't running,
    fails immediately instead of retrying for minutes.
    Default readonly=True for safety on live accounts.
    """
    global _portfolio_ib
    _ensure_event_loop()

    if _portfolio_ib is not None and _portfolio_ib.isConnected():
        return _portfolio_ib

    # Quick check: is the port even open?
    if not _is_port_open(host, port):
        raise ConnectionError(
            f"Portfolio TWS not reachable on {host}:{port}. "
            f"Is TWS for the portfolio account running with API enabled on port {port}?"
        )

    _portfolio_ib = IB()

    # IBKR info codes that are NOT errors (2104=farm OK, 2106=HMDS OK, 2158=secdef OK)
    _info_codes = {2104, 2106, 2107, 2108, 2158}

    def on_error(reqId, errorCode, errorString, contract):
        if errorCode in _info_codes:
            log.debug("portfolio_ibkr_info", code=errorCode, msg=errorString)
        elif errorCode >= 2000:
            log.error("portfolio_ibkr_error", code=errorCode, msg=errorString, req_id=reqId)

    _portfolio_ib.errorEvent += on_error

    log.info("portfolio_connecting_ibkr", host=host, port=port, client_id=client_id, readonly=readonly)

    for attempt in range(1, 3):  # max 2 attempts (don't block forever)
        try:
            _portfolio_ib.connect(
                host=host, port=port, clientId=client_id,
                timeout=30, readonly=readonly, account=account,
            )
            _portfolio_ib.RequestTimeout = 15
            if account:
                _portfolio_ib.reqAccountUpdates(account=account)
            else:
                _portfolio_ib.reqAccountUpdates()
            log.info("portfolio_ibkr_connected",
                     accounts=_portfolio_ib.managedAccounts(),
                     readonly=readonly)
            return _portfolio_ib
        except Exception as e:
            wait = min(5 * (2 ** (attempt - 1)), 120)
            log.warning("portfolio_ibkr_connect_failed",
                        attempt=attempt,
                        error=str(e) or repr(e),
                        type=type(e).__name__)
            if attempt < 2:
                time.sleep(wait)

    raise ConnectionError("Failed to connect portfolio to IBKR")


def disconnect_portfolio():
    global _portfolio_ib
    if _portfolio_ib and _portfolio_ib.isConnected():
        _portfolio_ib.disconnect()
        log.info("portfolio_ibkr_disconnected")
    _portfolio_ib = None


def is_portfolio_connected() -> bool:
    return _portfolio_ib is not None and _portfolio_ib.isConnected()


# ── Cached portfolio account data for dashboard ──
import threading as _threading
_cached_portfolio_account: dict = {}
_portfolio_cache_lock = _threading.Lock()
_CACHE_FILE = "data/portfolio_account_cache.json"


def refresh_portfolio_account_cache():
    """Refresh using module-level connection.
    Also refreshes accrued interest from Flex Query even if IB not connected."""
    global _portfolio_ib
    if _portfolio_ib and _portfolio_ib.isConnected():
        refresh_portfolio_account_cache_from(_portfolio_ib)
    else:
        # IB not connected — still refresh accrued interest from Flex Query
        try:
            from src.portfolio.capital_injections import fetch_accrued_interest_usd
            interest = fetch_accrued_interest_usd()
            with _portfolio_cache_lock:
                _cached_portfolio_account["accrued_interest"] = interest
            try:
                import json as _json, os as _os
                _os.makedirs(_os.path.dirname(_CACHE_FILE), exist_ok=True)
                with open(_CACHE_FILE, "r") as f:
                    _data = _json.load(f)
                _data["accrued_interest"] = interest
                with open(_CACHE_FILE, "w") as f:
                    _json.dump(_data, f)
                log.info("accrued_interest_refreshed", value=round(interest, 2))
            except Exception as e:
                log.warning("accrued_interest_file_write_failed", error=str(e))
        except Exception as e:
            log.warning("accrued_interest_refresh_failed", error=str(e))


def refresh_portfolio_account_cache_from(ib):
    """Refresh cached portfolio account data from a given IB connection."""
    global _cached_portfolio_account
    try:
        if ib is None or not ib.isConnected():
            return
        values = ib.accountValues()
        if not values:
            return
        data = {}
        for v in values:
            if v.currency in ("BASE", "EUR", "USD"):
                if v.tag == "NetLiquidation" and v.currency in ("BASE", "USD"):
                    data["nlv"] = float(v.value)
                elif v.tag == "MaintMarginReq" and v.currency in ("BASE", "USD"):
                    data["margin"] = float(v.value)
                elif v.tag == "BuyingPower" and v.currency in ("BASE", "USD"):
                    data["buying_power"] = float(v.value)
        # FX rates from IBKR ExchangeRate tags
        fx_rates = {}
        for v in values:
            if v.tag == "ExchangeRate" and v.currency not in ("BASE",):
                try:
                    fx_rates[v.currency] = float(v.value)
                except Exception:
                    pass
        data["fx_rates"] = fx_rates

        # Loans: sum negative TotalCashBalance per currency, converted to USD
        try:
            def _fx(amount, currency):
                if currency in ("USD", "BASE"):
                    return amount
                rate = fx_rates.get(currency)
                return amount * rate if rate else amount
            loans = 0.0
            for v in values:
                if v.tag == "TotalCashBalance" and v.currency not in ("BASE",):
                    val = float(v.value)
                    if val < 0:
                        loans += _fx(val, v.currency)
            data["loans"] = loans
            # Accrued interest from Flex Query
            try:
                from src.portfolio.capital_injections import fetch_accrued_interest_usd
                data["accrued_interest"] = fetch_accrued_interest_usd()
            except Exception:
                data["accrued_interest"] = 0.0
        except Exception:
            data["loans"] = 0.0
            data["accrued_interest"] = 0.0
        if data.get("nlv", 0) > 0:
            data["margin_pct"] = (data.get("margin", 0) / data["nlv"]) * 100
        else:
            data["margin_pct"] = 0
        # Fetch BRK-B price history from IBKR
        try:
            from ib_insync import Stock as _Stock
            _brkb = _Stock("BRK B", "SMART", "USD")
            ib.qualifyContracts(_brkb)
            _bars = ib.reqHistoricalData(
                _brkb, endDateTime="",
                durationStr="365 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True,
                formatDate=1, timeout=15,
            )
            if _bars:
                data["brkb_history"] = {
                    str(b.date): float(b.close) for b in _bars
                }
        except Exception as e:
            log.warning("brkb_cache_fetch_failed", error=str(e))

        with _portfolio_cache_lock:
            _cached_portfolio_account = data
        try:
            import json, os
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            with open(_CACHE_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
    except Exception:
        pass


def get_cached_portfolio_account() -> dict:
    """Return cached portfolio account data (non-blocking, for dashboard)."""
    with _portfolio_cache_lock:
        if _cached_portfolio_account:
            return dict(_cached_portfolio_account)
    try:
        import json
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}




