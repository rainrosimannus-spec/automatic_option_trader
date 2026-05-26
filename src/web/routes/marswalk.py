"""
MarsWalk route — resilience-backtest dashboard.

Reads the isolated marswalk.db only. The config panel posts DTE/delta; Run-now
launches a background sweep across all regimes (engine + shared selection cores).
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.config import get_settings, StrategyConfig, RiskConfig
from src.marswalk.regimes import load_config
from src.marswalk.engine import Params
from src.marswalk.models import get_mw_db, Run, Point
from src.marswalk import service

router = APIRouter()


@router.get("/marswalk", response_class=HTMLResponse)
def marswalk_page(request: Request):
    import json
    universe, regimes = load_config()
    cards = []
    last_params = None  # most recent Run's params_json (across all regimes)
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
        # Pull the single most-recent run across the whole table to recover the
        # last submitted parameters, so the form persists what was just run
        # instead of snapping back to settings.yaml defaults.
        latest_run = db.query(Run).order_by(Run.created_at.desc()).first()
        if latest_run and latest_run.params_json:
            try:
                last_params = json.loads(latest_run.params_json)
            except Exception:
                last_params = None

    # "Run now as it is" — defaults mirror the LIVE aggressive son-mode config
    # we committed in c522a8e + hybrid wheel 63d8ed8. Read from the Pydantic
    # class defaults (committed Python intent) instead of get_settings(), so
    # the UI shows the canonical aggressive setup even on hosts where
    # settings.yaml still carries stale pre-aggressive overrides (octoserver
    # YAML migration is task #48). Put DTE is VIX-tiered in live; the US
    # low/mid-VIX tier is 0-3 (high-VIX halts).
    s = StrategyConfig()
    r = RiskConfig()
    defaults = {
        "dte_min": 0,
        "dte_max": 3,
        "delta_min": s.delta_min,
        "delta_max": s.delta_max,
        "put_min_premium": s.min_premium_put,
        "cc_dte_min": s.cc_dte_min,
        "cc_dte_max": s.cc_dte_max,
        "cc_delta_min": s.cc_delta_min,
        "cc_delta_max": s.cc_delta_max,
        "cc_min_premium": s.min_premium,
        # Account & deployment knobs — son's actual current NLV.
        "start_nlv": 34224,
        "collateral_cap_pct": round(r.total_exposure_pct * 100, 1),
        "uplift_k": 1.0,
        "gap_stress_pct": 0,
        # Live trades with IBKR portfolio margin → on by default.
        "margin_on": True,
        "margin_multiple": 5.0,
        "max_positions": r.max_portfolio_positions,
        # Risk gates — aggressive son-mode values.
        "iv_rank_min": s.iv_rank_min,
        "vix_halt": r.vix_pause_threshold,
        # Son-mode 60% margin ceiling.
        "max_margin_usage_pct": round(r.max_margin_usage * 100, 1),
    }
    if last_params:
        for k, v in last_params.items():
            if k in defaults and v is not None:
                defaults[k] = v
        # Re-derive the UI fields that aren't 1:1 with engine Params.
        if "total_exposure_pct" in last_params and last_params["total_exposure_pct"]:
            defaults["collateral_cap_pct"] = round(last_params["total_exposure_pct"] * 100, 2)
        if "short_dte_uplift_k" in last_params:
            defaults["uplift_k"] = last_params["short_dte_uplift_k"]
        if "gap_stress" in last_params:
            defaults["gap_stress_pct"] = round(last_params["gap_stress"] * 100, 2)
        if "start_capital" in last_params:
            defaults["start_nlv"] = last_params["start_capital"]
        if "max_margin_usage" in last_params and last_params["max_margin_usage"]:
            defaults["max_margin_usage_pct"] = round(last_params["max_margin_usage"] * 100, 2)

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
    margin_on: str = Form(""), margin_multiple: float = Form(5.0),
    max_positions: int = Form(10),
    iv_rank_min: float = Form(20.0), vix_halt: float = Form(30.0),
    max_margin_usage_pct: float = Form(80.0),
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
        margin_on=bool(margin_on),
        margin_multiple=max(1.0, min(10.0, margin_multiple)),
        max_positions=max(1, min(200, max_positions)),
        iv_rank_min=max(0.0, min(100.0, iv_rank_min)),
        vix_halt=max(10.0, min(100.0, vix_halt)),
        max_margin_usage=max(0.05, min(2.0, max_margin_usage_pct / 100.0)),
    )
    service.run_all_async(params, fetch=True)
    return RedirectResponse(url="/marswalk", status_code=303)
