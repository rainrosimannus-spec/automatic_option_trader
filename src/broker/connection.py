"""
IBKR connection manager — connect, reconnect, health checks.
Uses ib_insync for all broker communication.

Connection strategy:
- get_ib() returns the existing connection or raises ConnectionError.
  It NEVER attempts to reconnect — this prevents multiple jobs from
  fighting over the same client ID simultaneously.
- Only the health check job calls reconnect() to restore connectivity.
- Scanners and trade_sync use their own dedicated client IDs.
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
_ib_lock = threading.Lock()

# IBKR info codes that are NOT errors (farm status notifications)
_INFO_CODES = {
    2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158,
    # 2103 = farm broken, 2104 = farm OK, 2105 = HMDS broken,
    # 2106 = HMDS OK, 2158 = secdef OK — all just status updates
}


def is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP check — is TWS/Gateway listening on this port?
    Returns immediately if not, avoiding long retry loops."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def get_ib() -> IB:
    """Return the singleton IB connection if connected, otherwise raise.

    This function NEVER reconnects. If the connection is down, it raises
    ConnectionError immediately. The health check job is responsible for
    calling reconnect() to restore the connection. This prevents multiple
    jobs from simultaneously trying to reconnect with the same client ID.
    """
    global _ib
    if _ib is not None and _ib.isConnected():
        return _ib
    raise ConnectionError("IBKR not connected — waiting for health check to reconnect")


def get_ib_lock() -> threading.Lock:
    """Return the IB connection lock for serializing requests."""
    return _ib_lock


def initial_connect() -> IB:
    """Connect at startup. Only called once from main.py."""
    global _ib
    _ib = _connect(max_retries=5)
    return _ib


def _connect(max_retries: int = 3) -> IB:
    """
    Establish connection to IBKR TWS / Gateway.

    Does a quick TCP port check first — if TWS isn't running at all,
    fails immediately instead of burning through retries.
    Retries with exponential backoff only if TWS seems to be starting up.
    """
    cfg = get_settings().ibkr

    # Quick check: is the port even open?
    if not is_port_open(cfg.host, cfg.port):
        raise ConnectionError(
            f"Options Trader TWS not reachable on {cfg.host}:{cfg.port}. "
            f"Is TWS for account {cfg.account} running with API enabled on port {cfg.port}?"
        )

    ib = IB()
    ib.errorEvent += _on_error

    log.info(
        "connecting_to_ibkr",
        host=cfg.host,
        port=cfg.port,
        client_id=cfg.client_id,
        readonly=cfg.readonly,
    )

    for attempt in range(1, max_retries + 1):
        try:
            ib.connect(
                host=cfg.host,
                port=cfg.port,
                clientId=cfg.client_id,
                timeout=cfg.timeout,
                readonly=cfg.readonly,
                account=cfg.account or "",
            )
            ib.RequestTimeout = 15

            # Set market data type to delayed frozen (4) — works without
            # paid streaming subscriptions, compatible with reqHistoricalData
            ib.reqMarketDataType(4)

            # Give TWS a moment to send initial farm status messages
            ib.sleep(2)

            log.info("ibkr_connected",
                     account=ib.managedAccounts(),
                     readonly=cfg.readonly)
            return ib
        except Exception as e:
            # Exponential backoff: 5, 10, 20 seconds
            wait = min(5 * (2 ** (attempt - 1)), 30)
            log.warning("ibkr_connect_failed",
                        attempt=attempt, max=max_retries,
                        error=str(e), retry_in=wait)
            if attempt < max_retries:
                # Disconnect cleanly before retry
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
    # Disconnect existing
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
        # Farm status messages — just debug, not errors
        log.debug("ibkr_farm_status", code=errorCode, msg=errorString)
    elif errorCode < 2000:
        log.debug("ibkr_warning", code=errorCode, msg=errorString)
    else:
        log.error("ibkr_error", code=errorCode, msg=errorString, req_id=reqId)
