"""
Screener "running" flag — one file, written by WHOEVER runs the monthly screen.

The Screener page shows a "running in background" card off this flag. It used to be written
only by the dashboard's manual Run-now route, so every SCHEDULED first-Monday run (the normal
case) was invisible on the page. The flag now lives here so job_portfolio_monthly_screen sets
and clears it itself, and the route merely reads it.
"""
from __future__ import annotations

import time
from pathlib import Path

from src.core.logger import get_logger

log = get_logger(__name__)

RUNNING_FLAG_PATH = Path("data/screener_running.flag")
STALE_AFTER_SECONDS = 2 * 3600  # 2 hours — clear stale flag if the run died without cleanup


def set_running_flag() -> None:
    try:
        RUNNING_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNNING_FLAG_PATH.write_text(str(int(time.time())))
    except Exception as e:
        log.warning("screener_flag_write_failed", error=str(e))


def clear_running_flag() -> None:
    try:
        RUNNING_FLAG_PATH.unlink(missing_ok=True)
    except Exception as e:
        log.warning("screener_flag_clear_failed", error=str(e))


def is_screener_running() -> bool:
    """True if flag file exists AND is not stale (>2h old)."""
    if not RUNNING_FLAG_PATH.exists():
        return False
    try:
        started_at = int(RUNNING_FLAG_PATH.read_text().strip())
        age = time.time() - started_at
        if age > STALE_AFTER_SECONDS:
            log.warning("screener_flag_stale_clearing", age_seconds=int(age))
            clear_running_flag()
            return False
        return True
    except Exception:
        # Unreadable flag — clear it
        clear_running_flag()
        return False
