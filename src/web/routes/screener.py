"""
Screener route — read-only log of monthly universe screener runs.
Shows changes only: additions, removals, reclassifications, suggestions created.
The actual screening runs automatically on the first Monday of each month at 2 AM ET.
"""
from __future__ import annotations

import json
import threading
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


def _load_run_log() -> dict:
    if not RUN_LOG_PATH.exists():
        return {}
    try:
        return json.loads(RUN_LOG_PATH.read_text())
    except Exception:
        return {}


def _next_first_monday() -> str:
    from datetime import date, timedelta
    today = date.today()
    for months_ahead in range(0, 3):
        month = today.month + months_ahead
        year = today.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        first = date(year, month, 1)
        days_until_monday = (7 - first.weekday()) % 7
        first_monday = first + timedelta(days=days_until_monday)
        if first_monday > today:
            return first_monday.strftime("%B %d, %Y")
    return "Unknown"


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
    next_run = _next_first_monday()
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
        "universe_counts": universe_counts,
        "options_count": options_count,
        "universe_exists": SCREENED_UNIVERSE_PATH.exists(),
    })


@router.post("/screener/run-now")
async def run_screener_now(request: Request):
    from src.core.config import get_settings
    cfg = get_settings().portfolio
    if not cfg.enabled:
        return HTMLResponse('<div class="text-red-400">Portfolio not enabled.</div>')

    def _run():
        from src.portfolio.connection import get_portfolio_lock
        from src.portfolio.scheduler import job_portfolio_monthly_screen
        with get_portfolio_lock():
            job_portfolio_monthly_screen(cfg)

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
