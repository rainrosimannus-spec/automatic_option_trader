"""
Watchlist & Buy-Signals route — the compounder strategy's live view.

Reads the per-name signal table and reserve state that PortfolioBuyer.run_compounder_scan
persists to PortfolioState (keys: compounder_signals, compounder_*). Pure read of the DB —
no IBKR needed to render (it shows the last scan's ranking/targets/actions).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.portfolio.models import PortfolioState
from src.core.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


def _state(key: str, default: str = "") -> str:
    try:
        with get_db() as db:
            s = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            return s.value if s and s.value is not None else default
    except Exception:
        return default


def _num(key: str, default: float = 0.0) -> float:
    try:
        return float(_state(key) or default)
    except Exception:
        return default


@router.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    try:
        signals = json.loads(_state("compounder_signals") or "[]")
    except Exception:
        signals = []

    # Per-tier summary (count + intended target $)
    tier_summary: dict[str, dict] = {}
    for s in signals:
        t = s.get("tier", "growth")
        d = tier_summary.setdefault(t, {"count": 0, "target": 0.0, "deployed": 0.0})
        d["count"] += 1
        d["target"] += s.get("target", 0) or 0
        d["deployed"] += s.get("current", 0) or 0

    reserve = {
        "drawdown_pct": _num("compounder_drawdown_pct"),
        "tranches_fired": int(_num("compounder_tranches_fired")),
        "unlocked_pct": _num("compounder_reserve_unlocked_pct"),
        "investable": _num("compounder_investable"),
        "live_target": _num("compounder_live_target"),
        "deployed": _num("compounder_deployed"),
        "daily_budget": _num("compounder_daily_budget"),
        "reserve_peak": _num("compounder_reserve_peak"),
    }
    strategy = _state("strategy") or "classic"

    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "signals": signals,
        "tier_summary": tier_summary,
        "reserve": reserve,
        "strategy": strategy,
        "is_compounder": strategy == "compounder",
    })
