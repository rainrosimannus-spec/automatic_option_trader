"""
Portfolio IBKR connection — ONE connection to the portfolio gateway, serialized access.

Architecture mirrors broker/connection.py:
- Single IB connection (clientId=99) handles ALL portfolio operations.
- get_portfolio_ib() returns the singleton or raises — never reconnects itself.
- job_portfolio_health_check() is the only place that reconnects.
- initial_connect_portfolio() called once from main.py at startup.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Optional

from ib_insync import IB

from src.core.logger import get_logger

log = get_logger(__name__)

_portfolio_ib: Optional[IB] = None
_portfolio_lock = threading.RLock()
_portfolio_main_loop = None

_INFO_CODES = {2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158}


def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _ensure_event_loop():
    global _portfolio_main_loop
    if _portfolio_main_loop is not None:
        asyncio.set_event_loop(_portfolio_main_loop)
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed() or threading.current_thread() is not threading.main_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def _on_error(reqId: int, errorCode: int, errorString: str, contract) -> None:
    if errorCode in _INFO_CODES:
        log.debug("portfolio_ibkr_info", code=errorCode, msg=errorString)
    elif errorCode < 2000:
        log.debug("portfolio_ibkr_warning", code=errorCode, msg=errorString)
    else:
        log.error("portfolio_ibkr_error", code=errorCode, msg=errorString, req_id=reqId)


def get_portfolio_ib() -> IB:
    """Return the singleton portfolio IB connection if connected, otherwise raise.
    NEVER reconnects — health check job handles that."""
    global _portfolio_ib
    if _portfolio_ib is not None and _portfolio_ib.isConnected():
        return _portfolio_ib
    raise ConnectionError("Portfolio IBKR not connected — waiting for health check to reconnect")


def get_portfolio_lock() -> threading.RLock:
    return _portfolio_lock


def is_portfolio_connected() -> bool:
    return _portfolio_ib is not None and _portfolio_ib.isConnected()


def initial_connect_portfolio() -> IB:
    """Connect at startup. Only called once from main.py."""
    global _portfolio_ib
    _portfolio_ib = _connect(max_retries=5)
    return _portfolio_ib


def _connect(max_retries: int = 3) -> IB:
    from src.core.config import get_settings
    cfg = get_settings().portfolio
    _ensure_event_loop()

    if not _is_port_open(cfg.ibkr_host, cfg.ibkr_port):
        raise ConnectionError(
            f"Portfolio TWS not reachable on {cfg.ibkr_host}:{cfg.ibkr_port}"
        )

    ib = IB()
    ib.errorEvent += _on_error

    log.info("portfolio_connecting_ibkr",
             host=cfg.ibkr_host, port=cfg.ibkr_port,
             client_id=cfg.ibkr_client_id)

    for attempt in range(1, max_retries + 1):
        try:
            ib.connect(
                host=cfg.ibkr_host,
                port=cfg.ibkr_port,
                clientId=cfg.ibkr_client_id,
                timeout=30,
                readonly=True,
                account=cfg.ibkr_account or "",
            )
            ib.RequestTimeout = 15
            ib.reqMarketDataType(4)
            ib.sleep(2)

            global _portfolio_main_loop
            _portfolio_main_loop = asyncio.get_event_loop()

            log.info("portfolio_connection_established",
                     accounts=ib.managedAccounts(),
                     clientId=cfg.ibkr_client_id)

            try:
                refresh_portfolio_account_cache_from(ib)
            except Exception as e:
                log.warning("portfolio_initial_cache_failed", error=str(e))

            try:
                refresh_brkb_history(ib)
            except Exception as e:
                log.warning("portfolio_initial_brkb_failed", error=str(e))

            try:
                from src.portfolio.sync import sync_ibkr_holdings
                sync_ibkr_holdings(ib)
            except Exception as e:
                log.warning("portfolio_holdings_sync_failed", error=str(e))

            # Subscribe to account updates to keep connection alive.
            # Without this, IBKR drops the read-only connection after ~60s idle.
            try:
                ib.reqAccountUpdates(True, cfg.ibkr_account)
                log.info("portfolio_account_updates_subscribed",
                         account=cfg.ibkr_account)
            except Exception as e:
                log.warning("portfolio_account_updates_failed", error=str(e))

            return ib

        except Exception as e:
            wait = min(5 * (2 ** (attempt - 1)), 30)
            log.warning("portfolio_connect_retry",
                        attempt=attempt, max=max_retries,
                        error=str(e), retry_in=wait)
            if attempt < max_retries:
                try:
                    ib.disconnect()
                except Exception:
                    pass
                time.sleep(wait)
            else:
                raise ConnectionError(
                    f"Failed to connect portfolio to IBKR after {max_retries} attempts: {e}"
                ) from e

    raise ConnectionError("Unreachable")


def disconnect_portfolio() -> None:
    global _portfolio_ib
    if _portfolio_ib and _portfolio_ib.isConnected():
        _portfolio_ib.disconnect()
        log.info("portfolio_ibkr_disconnected")
    _portfolio_ib = None


def reconnect_portfolio() -> IB:
    """Force a fresh reconnection. Only called by health check job."""
    global _portfolio_ib, _portfolio_main_loop
    if _portfolio_ib:
        try:
            _portfolio_ib.disconnect()
        except Exception:
            pass
    _portfolio_ib = None
    _portfolio_main_loop = None  # force fresh event loop — stale loop causes silent reconnect failure
    _portfolio_ib = _connect(max_retries=3)
    return _portfolio_ib


# ── Cached portfolio account data for dashboard ──────────────────────────────

_cached_portfolio_account: dict = {}
_portfolio_cache_lock = threading.Lock()
_CACHE_FILE = "data/portfolio_account_cache.json"


def refresh_portfolio_account_cache():
    global _portfolio_ib
    if _portfolio_ib and _portfolio_ib.isConnected():
        refresh_portfolio_account_cache_from(_portfolio_ib)
    else:
        try:
            from src.portfolio.capital_injections import fetch_accrued_interest_usd, fetch_dividends_ytd_usd
            interest = fetch_accrued_interest_usd()
            dividends_ytd = fetch_dividends_ytd_usd()
            with _portfolio_cache_lock:
                _cached_portfolio_account["accrued_interest"] = interest
                _cached_portfolio_account["dividends_ytd"] = dividends_ytd
            try:
                import json as _json, os as _os
                _os.makedirs(_os.path.dirname(_CACHE_FILE), exist_ok=True)
                with open(_CACHE_FILE, "r") as f:
                    _data = _json.load(f)
                if interest != 0.0:
                    _data["accrued_interest"] = interest
                if dividends_ytd != 0.0:
                    _data["dividends_ytd"] = dividends_ytd
                with open(_CACHE_FILE, "w") as f:
                    _json.dump(_data, f)
                log.info("accrued_interest_refreshed", value=round(interest, 2))
                log.info("dividends_ytd_refreshed", value=round(dividends_ytd, 2))
            except Exception as e:
                log.warning("accrued_interest_file_write_failed", error=str(e))
        except Exception as e:
            log.warning("accrued_interest_refresh_failed", error=str(e))


def refresh_portfolio_account_cache_from(ib: IB):
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
                elif v.tag == "UnrealizedPnL" and v.currency in ("BASE", "USD"):
                    data["unrealized_pnl"] = float(v.value)

        fx_rates = {}
        for v in values:
            if v.tag == "ExchangeRate" and v.currency not in ("BASE",):
                try:
                    fx_rates[v.currency] = float(v.value)
                except Exception:
                    pass
        data["fx_rates"] = fx_rates

        try:
            def _fx(amount, currency):
                if currency in ("USD", "BASE"):
                    return amount
                rate = fx_rates.get(currency)
                return amount * rate if rate else amount

            loans = 0.0
            for v in values:
                if v.tag == "TotalCashBalance" and v.currency == "BASE":
                    val = float(v.value)
                    if val < 0:
                        loans = val
                    break
            data["loans"] = loans

            accrued_dividends = 0.0
            for v in values:
                if v.tag == "AccruedDividend" and v.currency not in ("BASE",):
                    try:
                        accrued_dividends += _fx(float(v.value), v.currency)
                    except Exception:
                        pass
            data["accrued_dividends"] = accrued_dividends

            accrued_dividends = 0.0
            for v in values:
                if v.tag == "AccruedDividend" and v.currency not in ("BASE",):
                    try:
                        accrued_dividends += _fx(float(v.value), v.currency)
                    except Exception:
                        pass
            data["accrued_dividends"] = accrued_dividends

            try:
                import json as _json
                with open(_CACHE_FILE, "r") as _f:
                    _cached = _json.load(_f)
                data["accrued_interest"] = _cached.get("accrued_interest", 0.0)
                data["dividends_ytd"] = _cached.get("dividends_ytd", 0.0)
            except Exception:
                data["accrued_interest"] = 0.0
                data["dividends_ytd"] = 0.0
        except Exception:
            data["loans"] = 0.0
            data["accrued_interest"] = 0.0
            data["dividends_ytd"] = 0.0

        if data.get("nlv", 0) > 0:
            data["margin_pct"] = (data.get("margin", 0) / data["nlv"]) * 100
        else:
            data["margin_pct"] = 0

        with _portfolio_cache_lock:
            _cached_portfolio_account = data

        try:
            import json, os
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            # Preserve brkb_history from existing cache — it's written by refresh_brkb_history()
            try:
                with open(_CACHE_FILE, "r") as f:
                    existing = json.load(f)
                if "brkb_history" in existing and "brkb_history" not in data:
                    data["brkb_history"] = existing["brkb_history"]
            except Exception:
                pass
            with open(_CACHE_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    except Exception:
        pass


def refresh_brkb_history(ib: IB):
    """Fetch BRK-B 1-year daily history via IBKR and store in cache.
    Called once at startup and daily — NOT in the health check."""
    try:
        from ib_insync import Stock as _Stock
        _brkb = _Stock("BRK B", "SMART", "USD")
        _bars = ib.reqHistoricalData(
            _brkb, endDateTime="",
            durationStr="365 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True,
            formatDate=1, timeout=15,
        )
        if _bars:
            brkb_data = {str(b.date): float(b.close) for b in _bars}
            with _portfolio_cache_lock:
                _cached_portfolio_account["brkb_history"] = brkb_data
            try:
                import json as _json, os as _os
                _os.makedirs(_os.path.dirname(_CACHE_FILE), exist_ok=True)
                try:
                    with open(_CACHE_FILE, "r") as f:
                        existing = _json.load(f)
                except Exception:
                    existing = {}
                existing["brkb_history"] = brkb_data
                with open(_CACHE_FILE, "w") as f2:
                    _json.dump(existing, f2)
            except Exception:
                pass
            log.info("brkb_history_refreshed", entries=len(brkb_data))
    except Exception as e:
        log.warning("brkb_cache_fetch_failed", error=str(e))


def get_cached_portfolio_account() -> dict:
    with _portfolio_cache_lock:
        if _cached_portfolio_account:
            return dict(_cached_portfolio_account)
    try:
        import json
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ── Open orders cache (mirrors broker/orders.py pattern) ────
_cached_portfolio_orders: list = []
_portfolio_orders_lock = threading.Lock()


def refresh_portfolio_open_orders_cache() -> None:
    """
    Fetch open option positions from portfolio IBKR and cache for dashboard.
    Uses ib.positions() not openTrades() — the portfolio connection is read-only
    so openTrades() always returns empty. positions() returns all held positions
    regardless of how they were entered, which is what the dashboard needs.
    Only caches option positions (secType == OPT) — stock holdings shown separately.
    Preserves existing cache if not connected (shows last known state).
    """
    global _cached_portfolio_orders
    try:
        if not is_portfolio_connected():
            return  # preserve existing cache — don't wipe on transient disconnect
        ib = get_portfolio_ib()
        with _portfolio_lock:
            positions = ib.positions()
        new_cache = []
        for pos in positions:
            try:
                c = pos.contract
                if c.secType != "OPT":
                    continue
                # Determine action from position sign: negative = short (sold), positive = long (bought)
                position_size = pos.position
                action = "SELL" if position_size < 0 else "BUY"
                new_cache.append({
                    "order_id": None,
                    "symbol": c.symbol,
                    "sec_type": c.secType,
                    "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
                    "strike": getattr(c, "strike", ""),
                    "right": getattr(c, "right", ""),
                    "action": action,
                    "quantity": abs(position_size),
                    "order_type": "POSITION",
                    "limit_price": None,
                    "status": "Open",
                    "filled": abs(position_size),
                    "remaining": 0,
                    "avg_cost": getattr(pos, "avgCost", None),
                })
            except Exception:
                continue
        with _portfolio_orders_lock:
            _cached_portfolio_orders = new_cache
        log.info("portfolio_options_cache_refreshed", count=len(new_cache))
    except Exception as e:
        log.warning("portfolio_options_cache_failed", error=str(e))


def get_cached_portfolio_open_orders() -> list:
    """Return cached portfolio open orders (non-blocking, for dashboard)."""
    with _portfolio_orders_lock:
        return list(_cached_portfolio_orders)
