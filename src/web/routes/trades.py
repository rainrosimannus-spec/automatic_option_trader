"""
Trade history view.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.core.models import Trade

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def list_trades(request: Request, limit: int = Query(100, ge=1, le=1000)):
    with get_db() as db:
        trade_list = (
            db.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(limit)
            .all()
        )
    return templates.TemplateResponse("trades.html", {
        "request": request,
        "trades": trade_list,
    })


@router.post("/sync")
def sync_trades():
    """Manually trigger IBKR trade + position sync."""
    try:
        from src.broker.trade_sync import sync_ibkr_trades, sync_ibkr_positions
        sync_ibkr_trades()
        sync_ibkr_positions()
    except Exception:
        import traceback
        traceback.print_exc()
    return RedirectResponse(url="/trades", status_code=303)
