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
                    # Stored points include ~280d of pre-regime warmup (MA200
                    # + IV-rank baseline) during which the engine doesn't
                    # trade — that's why the chart had a long flat lead-in.
                    # Filter to the effective regime window (analog for forward
                    # scenarios, native otherwise).
                    eff_s, eff_e, _ = reg.effective_window(today)
                    in_window = [p for p in pts if eff_s <= p.date <= eff_e]
                    pts_to_show = in_window if in_window else pts
                    # Rebase the target curve so it starts at 0 at the first
                    # visible day. Engine computes target_pct as 24% compounded
                    # over days-since-engine-start, but the engine's start is
                    # ~280d BEFORE the regime window — so by the time the
                    # visible chart begins, target is already at ~19% absolute
                    # while actual is at 0%. The visual delta lied. Now both
                    # curves start at 0; what's plotted is the GAIN over the
                    # visible window vs the GAIN required at 24%/yr.
                    if pts_to_show:
                        actual_base = pts_to_show[0].return_pct
                        target_base = pts_to_show[0].target_pct
                    else:
                        actual_base = target_base = 0
                    chart = {
                        "labels": [p.date for p in pts_to_show],
                        "actual": [p.return_pct - actual_base for p in pts_to_show],
                        "target": [p.target_pct - target_base for p in pts_to_show],
                    }
            is_forward = str(reg.start) > today
            has_analog = is_forward and reg.historical_analog is not None
            # Annualize ONLY when window is long enough for the extrapolation
            # to be honest. Below this threshold, compounding the daily yield
            # to a year produces absurd numbers (e.g. carry_2024 18-day +9.9%
            # compounds to +573%/yr — mathematically valid but practically
            # meaningless). Short windows show raw % over Nd instead.
            ANNUAL_MIN_DAYS = 90
            eff_start, eff_end, _ = reg.effective_window(today)
            try:
                from datetime import date as _date
                days = max(1, (_date.fromisoformat(eff_end) - _date.fromisoformat(eff_start)).days)
            except Exception:
                days = 365
            final_annual_pct = None
            if (run and run.final_return_pct is not None
                    and days >= ANNUAL_MIN_DAYS):
                final_annual_pct = ((1 + run.final_return_pct / 100.0) ** (365.0 / days) - 1) * 100.0
            cards.append({
                "regime": reg, "run": run, "chart": chart,
                "is_forward": is_forward, "has_analog": has_analog,
                "days": days,
                "final_annual_pct": final_annual_pct,
                "target_annual_pct": 24.0,   # TARGET_ANNUAL in engine.py
                "short_window": days < ANNUAL_MIN_DAYS,
            })

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
    default_nlv = 4_000_000
    # Derive collateral_cap_pct + max_positions from the live NLV ramp (mirrors
    # risk._effective_total_exposure_pct + scheduler options-count ladder), not
    # the pre-ramp base. At $4M this lifts cap from 20% → 40%, which is what
    # the live trader would actually grant a $4M account. Otherwise the engine
    # would silently bind notional at $4M × 20% × 5x = $4M and flatline.
    ramp_cap_pct, ramp_max_pos = _nlv_ramp(default_nlv)
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
        # Account & deployment knobs — large-account stress test ($4M).
        "start_nlv": default_nlv,
        "collateral_cap_pct": ramp_cap_pct,
        # Lowered 2026-05-26 from 4.95 -> 1.0 (see pricing.SHORT_DTE_K docstring).
        "uplift_k": 1.0,
        "gap_stress_pct": 0,
        # Live trades with IBKR portfolio margin → on by default.
        "margin_on": True,
        "margin_multiple": 5.0,
        "max_positions": ramp_max_pos,
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


def _nlv_ramp(nlv: float) -> tuple[float, int]:
    """Return (collateral_cap_pct, max_positions) for a given NLV. Mirrors the
    JS mwApplyNlvRamp() ladder in templates/marswalk.html so server-rendered
    initial defaults match what the client would compute on user change.

    Why this matters: the engine treats params.total_exposure_pct > 0 as a
    fixed override and skips its own _exposure_ramp(prev_nlv) fallback. So if
    the form rendered the base 20% at $4M NLV, the engine would cap notional
    at $4M × 20% × 5x margin = $4M instead of the $4M × 40% × 5x = $8M the
    live system would actually grant a $4M account. This made backtests look
    flat — the cap was the binding constraint, not the strategy."""
    # Growth-mode 2026-05-26: cap converges with max_margin_usage (80%) at the
    # top tier — collateral and margin governors bind together. Smaller accounts
    # ramp up faster than before so the engine actually uses available capacity.
    if nlv >= 5_000_000:
        return 80.0, 75
    if nlv >= 4_000_000:
        return 80.0, 50
    if nlv >= 2_000_000:
        return 60.0, 50
    if nlv >=   500_000:
        return 40.0, 30
    if nlv >=   200_000:
        return 30.0, 15
    if nlv >=   100_000:
        return 25.0, 10
    if nlv >=    50_000:
        return 20.0,  8
    if nlv >=    25_000:
        return 20.0,  6
    return 20.0, 4


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


# ── Short-DTE pricing calibration ────────────────────────────────────────
# Run via curl: curl -X POST http://localhost:<port>/marswalk/calibrate-pricing
# Snapshots live chain mids vs BSM theoretical for 0-7 DTE OTM puts across
# the universe, writes data/pricing_calibration_<YYYYMMDD>.jsonl. Must run
# in-process (uses portfolio IB singleton per ibkr-access-in-process-locked).
import threading as _threading
_calibration_state = {"active": False, "msg": "idle", "result": None}


def _run_calibration_thread():
    from tools.calibrate_short_dte_pricing import run_calibration
    _calibration_state["active"] = True
    _calibration_state["msg"] = "running"
    try:
        result = run_calibration()
        _calibration_state["result"] = result
        _calibration_state["msg"] = "done"
    except Exception as e:
        _calibration_state["result"] = {"status": "error", "msg": str(e)}
        _calibration_state["msg"] = "error"
    finally:
        _calibration_state["active"] = False


@router.post("/marswalk/calibrate-pricing")
def calibrate_pricing():
    """Trigger the short-DTE pricing calibration snapshot in a background thread.
    Returns immediately; check /marswalk/calibrate-pricing/status for progress."""
    if _calibration_state["active"]:
        return {"status": "already_running", "msg": _calibration_state["msg"]}
    _threading.Thread(target=_run_calibration_thread, daemon=True).start()
    return {"status": "started"}


@router.get("/marswalk/calibrate-pricing/status")
def calibrate_pricing_status():
    """Snapshot of the last calibration run."""
    return dict(_calibration_state)
