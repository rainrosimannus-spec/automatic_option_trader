"""
Trade suggestions dashboard — review and approve/reject suggested trades.
Separate pages for Options Trader and Portfolio Manager suggestions.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.suggestions import (
    get_pending_suggestions, approve_suggestion, reject_suggestion,
    TradeSuggestion,
)
from src.core.database import get_db
from src.core.models import SystemState
from src.core.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


def _get_auto_approve_state(source: str) -> bool:
    """Check if auto-approve is ON for a given source (options/portfolio)."""
    key = f"auto_approve_{source}"
    with get_db() as db:
        state = db.query(SystemState).filter(SystemState.key == key).first()
        return state is not None and state.value == "true"


def _get_suggestions_by_source(source: str):
    """Get pending and recent suggestions filtered by source."""
    all_pending = get_pending_suggestions()
    # Exclude submitted — they are fetched separately and prepended below
    pending = [s for s in all_pending if s.source == source and s.status != "submitted"]

    # Also include "submitted" orders (sent to IBKR but not yet filled)
    with get_db() as db:
        submitted = db.query(TradeSuggestion).filter(
            TradeSuggestion.status == "submitted",
            TradeSuggestion.source == source,
        ).order_by(TradeSuggestion.created_at.desc()).all()
        # Prepend submitted orders to pending list so they show at top
        pending = submitted + pending

        from sqlalchemy import or_
        recent = db.query(TradeSuggestion).filter(
            TradeSuggestion.source == source,
            TradeSuggestion.status.in_(["approved", "rejected", "expired", "executed", "submitted", "cancelled"]),
        ).order_by(TradeSuggestion.created_at.desc()).limit(40).all()
        # Force-load all attributes before session closes
        for r in recent:
            _ = r.symbol, r.status, r.action, r.rank, r.quantity, r.limit_price, r.strike, r.expiry, r.review_note, r.reviewed_at, r.source, r.created_at, r.opt_exchange

    return pending, recent


# ── Portfolio Suggestions (/suggestions) ──────────────────
@router.get("/", response_class=HTMLResponse)
def suggestions_page(request: Request):
    pending, recent = _get_suggestions_by_source("portfolio")
    auto_approve = _get_auto_approve_state("portfolio")
    return templates.TemplateResponse("suggestions.html", {
        "request": request,
        "pending": pending,
        "recent": recent,
        "page_title": "Portfolio Suggestions",
        "source": "portfolio",
        "nav_active": "suggestions",
        "auto_approve": auto_approve,
    })


# ── Options Suggestions (/options-suggestions) ────────────
@router.get("/options", response_class=HTMLResponse)
def options_suggestions_page(request: Request):
    pending, recent = _get_suggestions_by_source("options")
    auto_approve = _get_auto_approve_state("options")
    return templates.TemplateResponse("suggestions.html", {
        "request": request,
        "pending": pending,
        "recent": recent,
        "page_title": "Options Suggestions",
        "source": "options",
        "nav_active": "options_suggestions",
        "auto_approve": auto_approve,
    })


# ── Auto-Approve Toggle ──────────────────────────────────
@router.post("/toggle-auto-approve")
def toggle_auto_approve(request: Request, source: str = Form("options")):
    key = f"auto_approve_{source}"
    with get_db() as db:
        state = db.query(SystemState).filter(SystemState.key == key).first()
        if state:
            new_val = "false" if state.value == "true" else "true"
            state.value = new_val
        else:
            new_val = "true"
            db.add(SystemState(key=key, value=new_val))

    log.info("auto_approve_toggled", source=source, enabled=new_val)

    # If turning ON, immediately approve+execute all pending suggestions for this source
    if new_val == "true":
        all_pending = get_pending_suggestions()
        for s in all_pending:
            if s.source == source:
                approve_suggestion(s.id, note="auto-approved")
                log.info("auto_approved_existing", id=s.id, symbol=s.symbol)

    if source == "options":
        return RedirectResponse(url="/suggestions/options", status_code=303)
    return RedirectResponse(url="/suggestions", status_code=303)


# ── Approve / Reject (shared) ────────────────────────────
@router.post("/approve/{suggestion_id}")
def approve(suggestion_id: int, request: Request, note: str = Form(""), source: str = Form("portfolio")):
    approve_suggestion(suggestion_id, note)
    if source == "options":
        return RedirectResponse(url="/suggestions/options", status_code=303)
    return RedirectResponse(url="/suggestions", status_code=303)


@router.post("/reject/{suggestion_id}")
def reject(suggestion_id: int, request: Request, note: str = Form(""), source: str = Form("portfolio")):
    reject_suggestion(suggestion_id, note)
    if source == "options":
        return RedirectResponse(url="/suggestions/options", status_code=303)
    return RedirectResponse(url="/suggestions", status_code=303)


def _cancel_live_order_for_suggestion(s: TradeSuggestion) -> int:
    """Cancel the live IBKR order(s) matching an already-SUBMITTED suggestion. Routes to the right
    account by source (portfolio = Winston, options = Maggy) and matches the order by symbol + type
    (+ option right/strike). Reads ib.trades() (incl. Inactive, which openTrades() hides) so a stuck
    order is reachable. Returns the count cancelled."""
    sym = (s.symbol or "").upper()
    act = s.action or ""
    is_opt = ("put" in act) or ("call" in act)
    want_right = "P" if "put" in act else ("C" if "call" in act else None)
    _DONE = {"Filled", "Cancelled", "ApiCancelled", "PendingCancel"}
    cancelled = 0
    try:
        if s.source == "portfolio":
            from src.portfolio.connection import (get_portfolio_ib, get_portfolio_lock,
                                                  is_portfolio_connected)
            if not is_portfolio_connected():
                log.warning("cancel_order_no_portfolio_conn", id=s.id, symbol=sym)
                return 0
            ib, lock = get_portfolio_ib(), get_portfolio_lock()
        else:
            from src.broker.connection import get_ib, get_ib_lock, is_connected
            if not is_connected():
                log.warning("cancel_order_no_options_conn", id=s.id, symbol=sym)
                return 0
            ib, lock = get_ib(), get_ib_lock()
        with lock:
            trades = list(ib.trades())
        for t in trades:
            c, o = getattr(t, "contract", None), getattr(t, "order", None)
            st = getattr(getattr(t, "orderStatus", None), "status", "") or ""
            if c is None or o is None or st in _DONE:
                continue
            if (getattr(c, "symbol", "") or "").upper() != sym:
                continue
            if is_opt:
                if getattr(c, "secType", "") != "OPT":
                    continue
                if want_right and getattr(c, "right", "") != want_right:
                    continue
                if s.strike and abs(float(getattr(c, "strike", 0) or 0) - float(s.strike)) > 1e-6:
                    continue
            else:
                if getattr(c, "secType", "") != "STK" \
                        or str(getattr(o, "action", "")).upper() != "BUY":
                    continue
            try:
                with lock:
                    ib.cancelOrder(o)
                cancelled += 1
                log.info("suggestion_order_cancelled", id=s.id, symbol=sym,
                         order_id=getattr(o, "orderId", None), status=st, source=s.source)
            except Exception as ce:
                log.warning("suggestion_order_cancel_failed", id=s.id, symbol=sym, error=str(ce))
    except Exception as e:
        log.warning("cancel_live_order_for_suggestion_failed", id=s.id, error=str(e))
    return cancelled


@router.post("/cancel/{suggestion_id}")
def cancel(suggestion_id: int, request: Request, source: str = Form("portfolio")):
    """Cancel the live IBKR order for a submitted suggestion and mark the card cancelled."""
    # Copy the fields the cancel helper needs into a detached object before the session closes.
    data = None
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
        if s:
            data = type("S", (), {"id": s.id, "symbol": s.symbol, "action": s.action,
                                  "source": s.source, "strike": s.strike})()
    if data is not None:
        n = _cancel_live_order_for_suggestion(data)
        with get_db() as db:
            s2 = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
            if s2:
                s2.status = "cancelled"
                s2.reviewed_at = datetime.utcnow()
                s2.review_note = (f"Order cancelled by user — {n} IBKR order(s) cancelled"
                                  if n else "Cancel requested — no live IBKR order found")
        log.info("suggestion_cancel_requested", id=suggestion_id, cancelled=n)
    if source == "options":
        return RedirectResponse(url="/suggestions/options", status_code=303)
    return RedirectResponse(url="/suggestions", status_code=303)
