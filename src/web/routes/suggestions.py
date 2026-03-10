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
    pending = [s for s in all_pending if s.source == source]

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
            or_(
                TradeSuggestion.status.in_(["approved", "rejected", "expired", "executed"]),
                # Pending with a review_note = margin-attempted, show in decisions
                (TradeSuggestion.status == "pending") & (TradeSuggestion.review_note.isnot(None)),
            )
        ).order_by(TradeSuggestion.created_at.desc()).limit(20).all()

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
