"""
IBKR connection manager — ONE connection per gateway, serialized access.

Architecture:
- Single IB connection (clientId from config) handles ALL operations:
  scans, orders, trade sync, market data, account queries.
- All access serialized through a threading lock to prevent conflicts.
- Health check job monitors and reconnects if needed.
- No separate scan/order/sync connections — eliminates pacing issues.
"""
from __future__ import annotations

import socket
import time
import threading
from typing import Optional

from ib_insync import IB

from src.core.config import get_settings
from src.core.logger import get_logger

log = get_logger(__name__)

_ib: Optional[IB] = None
_ib_lock = threading.RLock()  # RLock allows nested locking from same thread

# IBKR info codes that are NOT errors (farm status notifications)
_INFO_CODES = {
    2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158,
}


def is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP check — is TWS/Gateway listening on this port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def get_ib() -> IB:
    """Return the singleton IB connection if connected, otherwise raise.
    NEVER reconnects — health check job handles that."""
    global _ib
    if _ib is not None and _ib.isConnected():
        return _ib
    raise ConnectionError("IBKR not connected — waiting for health check to reconnect")


def get_ib_lock() -> threading.RLock:
    """Return the IB connection lock for serializing requests."""
    return _ib_lock


def initial_connect() -> IB:
    """Connect at startup. Only called once from main.py."""
    global _ib
    _ib = _connect(max_retries=5)
    return _ib


def _connect(max_retries: int = 3) -> IB:
    """Establish connection to IBKR TWS / Gateway."""
    cfg = get_settings().ibkr

    if not is_port_open(cfg.host, cfg.port):
        raise ConnectionError(
            f"Options Trader TWS not reachable on {cfg.host}:{cfg.port}. "
            f"Is TWS for account {cfg.account} running with API enabled on port {cfg.port}?"
        )

    ib = IB()
    ib.errorEvent += _on_error

    log.info("connecting_to_ibkr",
             host=cfg.host, port=cfg.port,
             client_id=cfg.client_id, readonly=cfg.readonly)

    for attempt in range(1, max_retries + 1):
        try:
            ib.connect(
                host=cfg.host, port=cfg.port,
                clientId=cfg.client_id,
                timeout=cfg.timeout,
                readonly=cfg.readonly,
                account=cfg.account or "",
            )
            ib.RequestTimeout = 15
            ib.reqMarketDataType(4)
            ib.sleep(2)

            log.info("ibkr_connected",
                     account=ib.managedAccounts(),
                     readonly=cfg.readonly)
            return ib
        except Exception as e:
            wait = min(5 * (2 ** (attempt - 1)), 30)
            log.warning("ibkr_connect_failed",
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
                    f"Failed to connect to IBKR after {max_retries} attempts: {e}"
                ) from e
    raise ConnectionError("Unreachable")


def disconnect() -> None:
    """Gracefully disconnect from IBKR."""
    global _ib
    if _ib and _ib.isConnected():
        _ib.disconnect()
        log.info("ibkr_disconnected")
    _ib = None


def is_connected() -> bool:
    return _ib is not None and _ib.isConnected()


def reconnect() -> IB:
    """Force a fresh reconnection. Only called by health check job."""
    global _ib
    if _ib:
        try:
            _ib.disconnect()
        except Exception:
            pass
    _ib = None
    _ib = _connect(max_retries=3)
    return _ib


def _on_error(reqId: int, errorCode: int, errorString: str, contract) -> None:
    """Handle IBKR error events."""
    if errorCode in _INFO_CODES:
        log.debug("ibkr_farm_status", code=errorCode, msg=errorString)
    elif errorCode < 2000:
        log.debug("ibkr_warning", code=errorCode, msg=errorString)
    else:
        log.error("ibkr_error", code=errorCode, msg=errorString, req_id=reqId)
