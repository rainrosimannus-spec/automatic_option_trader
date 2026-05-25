"""
MarsWalk route — resilience-backtest dashboard.

Reads the isolated marswalk.db only. The config panel posts DTE/delta; Run-now
launches a background sweep across all regimes (engine + shared selection cores).
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.config import get_settings
from src.marswalk.regimes import load_config
from src.marswalk.engine import Params
from src.marswalk.models import get_mw_db, Run, Point
from src.marswalk import service

router = APIRouter()


@router.get("/marswalk", response_class=HTMLResponse)
def marswalk_page(request: Request):
    universe, regimes = load_config()
    cards = []
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

    # Pre-fill with the LIVE system logic (settings.yaml strategy), so a backtest
    # starts from "what the trader does today". Put DTE is VIX-tiered (no single
    # value); the US low/mid-VIX tier is 0-3 (high-VIX halts), which matches the
    # US large-cap backtest universe.
    s = get_settings().strategy
    defaults = {
        "dte_min": 0,
        "dte_max": 3,
        "delta_min": getattr(s, "delta_min", 0.15),
        "delta_max": getattr(s, "delta_max", 0.30),
        "put_min_premium": getattr(s, "min_premium_put", 0.50),
        "cc_dte_min": getattr(s, "cc_dte_min", 5),
        "cc_dte_max": getattr(s, "cc_dte_max", 30),
        "cc_delta_min": getattr(s, "cc_delta_min", 0.15),
        "cc_delta_max": getattr(s, "cc_delta_max", 0.35),
        "cc_min_premium": getattr(s, "min_premium", 0.10),
        # Test configuration (sandbox knobs, not live config)
        "start_nlv": 100000,
        "collateral_cap_pct": 20,
        "uplift_k": 1.0,
        "gap_stress_pct": 0,
    }
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
    # Test configuration
    start_nlv: float = Form(100000), collateral_cap_pct: float = Form(20),
    uplift_k: float = Form(1.0), gap_stress_pct: float = Form(0),
):
    params = Params(
        dte_min=max(0, dte_min), dte_max=max(dte_min, dte_max),
        delta_min=max(0.01, delta_min), delta_max=max(delta_min, delta_max),
        put_min_premium=max(0.0, put_min_premium),
        cc_dte_min=max(0, cc_dte_min), cc_dte_max=max(cc_dte_min, cc_dte_max),
        cc_delta_min=max(0.01, cc_delta_min), cc_delta_max=max(cc_delta_min, cc_delta_max),
        cc_min_premium=max(0.0, cc_min_premium),
        start_capital=max(1000.0, start_nlv),
        total_exposure_pct=max(0.01, collateral_cap_pct / 100.0),
        short_dte_uplift_k=max(0.0, uplift_k),
        gap_stress=min(0.9, max(0.0, gap_stress_pct / 100.0)),
    )
    service.run_all_async(params, fetch=True)
    return RedirectResponse(url="/marswalk", status_code=303)
