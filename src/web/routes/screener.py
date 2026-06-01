"""
Screener route — read-only log of monthly universe screener runs.
Shows changes only: additions, removals, reclassifications, suggestions created.
The actual screening runs automatically on the first Monday of each month at 2 AM ET.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.logger import get_logger

log = get_logger(__name__)

router = APIRouter()

RUN_LOG_PATH = Path("data/screener_last_run.json")
SCREENED_UNIVERSE_PATH = Path("config/screened_universe.yaml")
OPTIONS_UNIVERSE_PATH = Path("config/options_universe.yaml")
RUNNING_FLAG_PATH = Path("data/screener_running.flag")
STALE_AFTER_SECONDS = 2 * 3600  # 2 hours — clear stale flag if thread died


def _set_running_flag() -> None:
    try:
        RUNNING_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNNING_FLAG_PATH.write_text(str(int(time.time())))
    except Exception as e:
        log.warning("screener_flag_write_failed", error=str(e))


def _clear_running_flag() -> None:
    try:
        RUNNING_FLAG_PATH.unlink(missing_ok=True)
    except Exception as e:
        log.warning("screener_flag_clear_failed", error=str(e))


def _is_screener_running() -> bool:
    """True if flag file exists AND is not stale (>2h old)."""
    if not RUNNING_FLAG_PATH.exists():
        return False
    try:
        started_at = int(RUNNING_FLAG_PATH.read_text().strip())
        age = time.time() - started_at
        if age > STALE_AFTER_SECONDS:
            log.warning("screener_flag_stale_clearing", age_seconds=int(age))
            _clear_running_flag()
            return False
        return True
    except Exception:
        # Unreadable flag — clear it
        _clear_running_flag()
        return False


def _load_run_log() -> dict:
    if not RUN_LOG_PATH.exists():
        return {}
    try:
        return json.loads(RUN_LOG_PATH.read_text())
    except Exception:
        return {}


def _first_monday_of(year: int, month: int):
    from datetime import date, timedelta
    first = date(year, month, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7)


def _parse_run_date(run_log: dict):
    # writer format: datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    s = (run_log or {}).get("run_date")
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M UTC").date()
    except Exception:
        return None


def _next_scheduled_run(run_log: dict) -> dict:
    """Returns {'date': 'June 01, 2026', 'overdue': bool}.

    Shows THIS month's first Monday until a successful run this month;
    only then rolls to next month's first Monday. An interrupted run (no
    log written) or an errored run keeps showing this month as pending.
    """
    from datetime import date
    today = date.today()
    fm_this = _first_monday_of(today.year, today.month)

    last = _parse_run_date(run_log)
    completed_this_month = (
        (run_log or {}).get("status") == "success"
        and last is not None
        and last >= fm_this
    )

    if not completed_this_month:
        return {"date": fm_this.strftime("%B %d, %Y"), "overdue": today > fm_this}

    ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    fm_next = _first_monday_of(ny, nm)
    return {"date": fm_next.strftime("%B %d, %Y"), "overdue": False}


def _load_universe_counts() -> dict:
    if not SCREENED_UNIVERSE_PATH.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(SCREENED_UNIVERSE_PATH.read_text())
        return {
            "breakthrough": len(data.get("breakthrough", [])),
            "growth": len(data.get("growth", [])),
            "dividend": len(data.get("dividend", [])),
        }
    except Exception:
        return {}


@router.get("/screener", response_class=HTMLResponse)
async def screener_page(request: Request):
    run_log = _load_run_log()
    _nr = _next_scheduled_run(run_log)
    next_run = _nr["date"]
    next_run_overdue = _nr["overdue"]
    universe_counts = _load_universe_counts()
    options_count = 0
    if OPTIONS_UNIVERSE_PATH.exists():
        try:
            import yaml
            data = yaml.safe_load(OPTIONS_UNIVERSE_PATH.read_text())
            options_count = len(data.get("stocks", []))
        except Exception:
            pass

    return templates.TemplateResponse("screener.html", {
        "request": request,
        "run_log": run_log,
        "next_run": next_run,
        "next_run_overdue": next_run_overdue,
        "universe_counts": universe_counts,
        "options_count": options_count,
        "universe_exists": SCREENED_UNIVERSE_PATH.exists(),
        "screener_running": _is_screener_running(),
    })


@router.post("/screener/run-now")
async def run_screener_now(request: Request):
    from src.core.config import get_settings
    cfg = get_settings().portfolio
    if not cfg.enabled:
        return HTMLResponse('<div class="text-red-400">Portfolio not enabled.</div>')

    if _is_screener_running():
        return HTMLResponse("""
        <div class="bg-amber-900/30 border border-amber-700 rounded-lg p-4">
            <div class="text-amber-400 font-medium">⏳ Screener is already running</div>
            <div class="text-sm text-gray-400 mt-1">
                A run is already in progress. Please wait for it to finish.
            </div>
        </div>
        """)

    _set_running_flag()

    def _run():
        try:
            from src.portfolio.connection import get_portfolio_lock, _ensure_event_loop
            from src.portfolio.scheduler import job_portfolio_monthly_screen
            _ensure_event_loop()
            with get_portfolio_lock():
                job_portfolio_monthly_screen(cfg)
        except Exception as e:
            log.error("screener_thread_failed", error=str(e))
        finally:
            _clear_running_flag()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return HTMLResponse("""
    <div class="bg-emerald-900/30 border border-emerald-700 rounded-lg p-4">
        <div class="text-emerald-400 font-medium">🔍 Monthly screener started in background</div>
        <div class="text-sm text-gray-400 mt-1">
            This will take 30-60 minutes. Refresh this page later to see results.
        </div>
    </div>
    """)
