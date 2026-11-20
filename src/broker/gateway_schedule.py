"""Options gateway daily restart window (2026-11-20).

The options IB Gateway restarts itself once a day at IBC's AutoRestartTime (07:00 Luxembourg since
2026-11-16, chosen to land after IBKR's 05:00 UTC server reset that kills the history farm). For a
few minutes after that the API port is gone and the dashboard header showed a bare red "IBKR
Offline" — which on 2026-11-20 earned a needless restart-all by hand (two IB Key pushes) two
minutes after the trader had already reconnected on its own.

This module knows WHEN that restart happens so the dashboard can say "daily gateway restart,
reconnecting" instead. The time is read from the IBC ini the launcher uses (~/ibc-config/
config-options.ini, `AutoRestartTime=07:00 AM`); the gateway evaluates it in its own TimeZone
(jts.ini = Europe/Luxembourg — confirmed by "Restart in progress" stamped 07:00:00 local on
11-17..11-20). Falls back to 07:00 if the file is unreadable. Options side only; the portfolio
gateway has its own schedule and is not touched here.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, time as dtime
from pathlib import Path

import pytz

GATEWAY_TZ = pytz.timezone("Europe/Luxembourg")
DEFAULT_INI = Path(os.environ.get("IBC_INI", str(Path.home() / "ibc-config" / "config-options.ini")))
DEFAULT_RESTART = dtime(7, 0)
GRACE_MINUTES = 10   # port down ~10 s, trader reconnect ≤ 5 min (health check) — 10 min covers both

_AUTO_RESTART_RE = re.compile(r"^\s*AutoRestartTime\s*=\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$", re.IGNORECASE)

_cache: dict = {"path": None, "mtime": None, "value": None}


def parse_auto_restart_time(text: str) -> dtime | None:
    """`AutoRestartTime=07:00 AM` → time(7, 0). 12-hour with AM/PM (IBC's format) or 24-hour.
    Returns None when the line is absent or malformed."""
    for line in text.splitlines():
        m = _AUTO_RESTART_RE.match(line.rstrip("\r"))
        if not m:
            continue
        hh, mm, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
        if ampm == "PM" and hh < 12:
            hh += 12
        elif ampm == "AM" and hh == 12:
            hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return dtime(hh, mm)
        return None
    return None


def read_auto_restart_time(path: Path | str | None = None) -> dtime:
    """The gateway's configured daily restart time (local to the gateway), cached on mtime.
    Unreadable / missing / malformed → DEFAULT_RESTART, never raises."""
    p = Path(path) if path else DEFAULT_INI
    try:
        mtime = p.stat().st_mtime
        if _cache["path"] == str(p) and _cache["mtime"] == mtime and _cache["value"] is not None:
            return _cache["value"]
        val = parse_auto_restart_time(p.read_text(errors="replace")) or DEFAULT_RESTART
        _cache.update(path=str(p), mtime=mtime, value=val)
        return val
    except Exception:
        return DEFAULT_RESTART


def restart_window_active(now_local: datetime, restart: dtime, grace_min: int = GRACE_MINUTES) -> bool:
    """True from the restart minute through `grace_min` minutes after it (same calendar day)."""
    start = now_local.replace(hour=restart.hour, minute=restart.minute, second=0, microsecond=0)
    delta = (now_local - start).total_seconds()
    return 0 <= delta < grace_min * 60


def gateway_restart_window(now: datetime | None = None, ini_path: Path | str | None = None) -> dict | None:
    """For the dashboard: {"start": "07:00", "until": "07:10", "tz": "Luxembourg"} while the daily
    options-gateway restart window is open, else None."""
    restart = read_auto_restart_time(ini_path)
    now_local = (now or datetime.now(pytz.UTC)).astimezone(GATEWAY_TZ)
    if not restart_window_active(now_local, restart):
        return None
    until_min = restart.hour * 60 + restart.minute + GRACE_MINUTES
    until = dtime((until_min // 60) % 24, until_min % 60)
    return {"start": restart.strftime("%H:%M"), "until": until.strftime("%H:%M"), "tz": "Luxembourg"}
