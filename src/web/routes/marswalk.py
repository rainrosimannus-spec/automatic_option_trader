"""
MarsWalk route — resilience-backtest dashboard.

Reads the isolated marswalk.db only. The config panel posts DTE/delta; Run-now
launches a background sweep across all regimes (engine + shared selection cores).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.marswalk.regimes import load_config
from src.marswalk.engine import Params
from src.marswalk.models import get_mw_db, Run, Point
from src.marswalk import service

router = APIRouter()


@router.get("/marswalk", response_class=HTMLResponse)
def marswalk_page(request: Request):
    universe, regimes = load_config()
    cards = []
    last = None
    with get_mw_db() as db:
        for reg in regimes:
            run = (db.query(Run).filter_by(regime_id=reg.id)
                   .order_by(Run.created_at.desc()).first())
            chart = None
            if run:
                pts = (db.query(Point).filter_by(run_id=run.id)
                       .order_by(Point.date).all())
                if pts:
                    chart = {
                        "labels": [p.date for p in pts],
                        "actual": [p.return_pct for p in pts],
                        "target": [p.target_pct for p in pts],
                    }
            cards.append({"regime": reg, "run": run, "chart": chart})
        last = db.query(Run).order_by(Run.created_at.desc()).first()

    defaults = {
        "dte_min": 5, "dte_max": 14, "delta_min": 0.15, "delta_max": 0.30,
        "put_min_premium": 0.0,
        "cc_dte_min": 5, "cc_dte_max": 21, "cc_delta_min": 0.20, "cc_delta_max": 0.40,
        "cc_min_premium": 0.0,
    }
    if last and last.params_json:
        try:
            saved = json.loads(last.params_json)
            defaults.update({k: v for k, v in saved.items() if k in defaults})
        except Exception:
            pass
    return templates.TemplateResponse("marswalk.html", {
        "request": request,
        "cards": cards,
        "defaults": defaults,
        "universe": universe,
        "running": service.is_running(),
        "status": service.status(),
    })


@router.post("/marswalk/run")
def marswalk_run(
    dte_min: int = Form(...), dte_max: int = Form(...),
    delta_min: float = Form(...), delta_max: float = Form(...),
    put_min_premium: float = Form(0.0),
    cc_dte_min: int = Form(...), cc_dte_max: int = Form(...),
    cc_delta_min: float = Form(...), cc_delta_max: float = Form(...),
    cc_min_premium: float = Form(0.0),
):
    params = Params(
        dte_min=max(0, dte_min), dte_max=max(dte_min, dte_max),
        delta_min=max(0.01, delta_min), delta_max=max(delta_min, delta_max),
        put_min_premium=max(0.0, put_min_premium),
        cc_dte_min=max(0, cc_dte_min), cc_dte_max=max(cc_dte_min, cc_dte_max),
        cc_delta_min=max(0.01, cc_delta_min), cc_delta_max=max(cc_delta_min, cc_delta_max),
        cc_min_premium=max(0.0, cc_min_premium),
    )
    service.run_all_async(params, fetch=True)
    return RedirectResponse(url="/marswalk", status_code=303)
