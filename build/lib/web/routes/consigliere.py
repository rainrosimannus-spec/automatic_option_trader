"""
Consigliere route — the advisor's dashboard page.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.consigliere.models import ConsigliereMemo

router = APIRouter()


@router.get("/consigliere", response_class=HTMLResponse)
def consigliere_page(request: Request):
    with get_db() as db:
        # Active memos (new + read)
        active = db.query(ConsigliereMemo).filter(
            ConsigliereMemo.status.in_(["new", "read"])
        ).order_by(
            # critical/warning first, then by date
            ConsigliereMemo.severity.desc(),
            ConsigliereMemo.created_at.desc(),
        ).all()

        # Dismissed memos (last 20)
        dismissed = db.query(ConsigliereMemo).filter(
            ConsigliereMemo.status == "dismissed"
        ).order_by(ConsigliereMemo.created_at.desc()).limit(20).all()

        # Stats
        total_memos = db.query(ConsigliereMemo).count()
        new_count = db.query(ConsigliereMemo).filter(
            ConsigliereMemo.status == "new"
        ).count()

        # Category breakdown
        from sqlalchemy import func
        category_counts = dict(
            db.query(ConsigliereMemo.category, func.count(ConsigliereMemo.id))
            .filter(ConsigliereMemo.status.in_(["new", "read"]))
            .group_by(ConsigliereMemo.category)
            .all()
        )

    return templates.TemplateResponse("consigliere.html", {
        "request": request,
        "active": active,
        "dismissed": dismissed,
        "total_memos": total_memos,
        "new_count": new_count,
        "category_counts": category_counts,
    })


@router.post("/consigliere/dismiss/{memo_id}")
def dismiss_memo(memo_id: int):
    with get_db() as db:
        memo = db.query(ConsigliereMemo).filter(
            ConsigliereMemo.id == memo_id
        ).first()
        if memo:
            memo.status = "dismissed"
            memo.dismissed_reason = "manually dismissed"
    return RedirectResponse(url="/consigliere", status_code=303)


@router.post("/consigliere/acknowledge/{memo_id}")
def acknowledge_memo(memo_id: int):
    with get_db() as db:
        memo = db.query(ConsigliereMemo).filter(
            ConsigliereMemo.id == memo_id
        ).first()
        if memo and memo.status == "new":
            memo.status = "read"
            memo.read_at = datetime.utcnow()
    return RedirectResponse(url="/consigliere", status_code=303)


@router.post("/consigliere/acted/{memo_id}")
def acted_on_memo(memo_id: int):
    with get_db() as db:
        memo = db.query(ConsigliereMemo).filter(
            ConsigliereMemo.id == memo_id
        ).first()
        if memo:
            memo.status = "acted_on"
    return RedirectResponse(url="/consigliere", status_code=303)


@router.post("/consigliere/run-review")
def trigger_review():
    """Manually trigger the Consigliere review."""
    from src.consigliere.advisor import Consigliere
    advisor = Consigliere()
    findings = advisor.run_daily_review()
    return RedirectResponse(url="/consigliere", status_code=303)
