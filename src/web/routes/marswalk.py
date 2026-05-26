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
    from datetime import date
    today = date.today().isoformat()
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
            is_forward = str(reg.start) > today
            cards.append({"regime": reg, "run": run, "chart": chart, "is_forward": is_forward})

    # "Run now as it is" — defaults mirror the LIVE aggressive son-mode config
    # we committed in c522a8e + hybrid wheel 63d8ed8. Read from the Pydantic
    # class defaults (committed Python intent) instead of get_settings(), so
    # the UI shows the canonical aggressive setup even on hosts where
    # settings.yaml still carries stale pre-aggressive overrides (octoserver
    # YAML migration is task #48). No DB-driven last_params override — the
    # form always starts at the canonical live config, never at stale prior
    # runs. Put DTE is VIX-tiered in live; the US low/mid-VIX tier is 0-3.
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
        # Account & deployment knobs — large-account stress test ($4M = scaled
        # NLV ramp tier where collateral cap lifts to 30% + max_positions=50).
        # The small-account exemption kicks in if you drop NLV below $100k×cap.
        "start_nlv": 4_000_000,
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

    return templates.TemplateResponse("marswalk.html", {
        "request": request,
        "cards": cards,
        "defaults": defaults,
        "universe": universe,
        "running": service.is_running(),
        "status": service.status(),
        "is_rth": _is_us_rth_now(),
    })


def _is_us_rth_now() -> bool:
    """NYSE regular trading hours check (Mon–Fri 09:30–16:00 ET, naive — no
    holiday/early-close detection). During RTH the live trader is hammering
    IBKR; the marswalk fetch contends for the portfolio lock and stalls. We
    flip to cached-only mode in that window."""
    from datetime import time
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return False  # fail open — keep prior fetch=True behavior
    from datetime import datetime as _dt
    now_et = _dt.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return time(9, 30) <= now_et.time() <= time(16, 0)


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
    # Auto-skip IBKR fetch during US RTH — the live trader saturates the
    # portfolio lock and the marswalk fetch otherwise stalls (45s per call ×
    # missing symbols). Off-hours, do the fresh fetch.
    fetch = not _is_us_rth_now()
    service.run_all_async(params, fetch=fetch)
    return RedirectResponse(url="/marswalk", status_code=303)
