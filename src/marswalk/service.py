"""
MarsWalk orchestration: run the chosen params across all regimes.

Run-now launches this in a background thread (the sweep is heavy). A nightly
off-hours scheduler job calls run_all_regimes directly.
"""
from __future__ import annotations

import threading

from src.core.logger import get_logger
from src.marswalk.regimes import load_config
from src.marswalk.engine import Params, run_regime, save_run
from src.marswalk import data as mw_data
from src.marswalk.models import get_mw_db, Run, Point

log = get_logger("marswalk.service")

# Simple single-flight status (one sweep at a time).
_state = {"active": False, "msg": "idle", "done": 0, "total": 0}


def is_running() -> bool:
    return _state["active"]


def status() -> dict:
    return dict(_state)


def _replace_prior(regime_id: str, params: Params):
    """Drop the previous run for this regime+param-set so the page shows one
    current curve per regime (avoids pile-up)."""
    with get_mw_db() as db:
        prior = db.query(Run).filter_by(
            regime_id=regime_id, dte_min=params.dte_min, dte_max=params.dte_max,
            delta_min=params.delta_min, delta_max=params.delta_max,
        ).all()
        for r in prior:
            db.query(Point).filter_by(run_id=r.id).delete()
            db.delete(r)


def run_all_regimes(params: Params, fetch: bool = True):
    """Synchronous sweep over all regimes under one param set."""
    if _state["active"]:
        log.info("marswalk_sweep_already_running")
        return
    universe, regimes = load_config()
    _state.update(active=True, msg="starting", done=0, total=len(regimes))
    try:
        for reg in regimes:
            _state["msg"] = f"running {reg.id}"
            try:
                if fetch:
                    mw_data.ensure_market_data(reg, universe)
                market = mw_data.load_market(reg, universe)
                if not market:
                    log.warning("marswalk_no_data", regime=reg.id)
                    _state["done"] += 1
                    continue
                earnings = mw_data.load_earnings(universe)
                res = run_regime(reg.id, reg.name, reg.category, reg.rank,
                                 universe, market, params, earnings=earnings)
                if res:
                    _replace_prior(reg.id, params)
                    save_run(res)
                    log.info("marswalk_run_done", regime=reg.id,
                             final=res["final_return_pct"], target=res["target_return_pct"])
            except Exception as e:
                log.warning("marswalk_regime_failed", regime=reg.id, error=str(e))
            _state["done"] += 1
        _state["msg"] = "done"
    finally:
        _state["active"] = False


def run_all_async(params: Params, fetch: bool = True):
    threading.Thread(target=run_all_regimes, args=(params, fetch), daemon=True).start()
