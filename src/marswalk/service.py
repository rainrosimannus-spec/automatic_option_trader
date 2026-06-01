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
from src.marswalk.synthetic import apply_halts, apply_shocks
from src.marswalk.models import get_mw_db, Run, Point

log = get_logger("marswalk.service")

# Simple single-flight status (one sweep at a time).
_state = {"active": False, "msg": "idle", "done": 0, "total": 0}

# Signature of the last config we auto-launched a sweep for. Guards the GET-side
# "auto-run first time" so a config with no stored runs triggers exactly one sweep
# per process-life — even if some regimes error out and never produce a matching
# row, we don't re-launch on every page refresh. Manual "Run now" bypasses this.
_last_autorun_sig: str | None = None


def is_running() -> bool:
    return _state["active"]


def status() -> dict:
    return dict(_state)


def should_autorun(sig: str) -> bool:
    """Return True (and record `sig`) if we have NOT already auto-launched a sweep
    for this config signature this process. Records atomically so concurrent
    page-loads don't double-launch. Manual Run-now does not touch this guard."""
    global _last_autorun_sig
    if _last_autorun_sig == sig:
        return False
    _last_autorun_sig = sig
    return True


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
                # Per-regime universe extension (e.g. broad-market names added
                # to gfc_2008/debt_2011/flash_2010 to test sector-diversified
                # wheel survival in pre-2015 windows). None for most regimes.
                ext = reg.universe_extension or []
                eff_universe = list(universe) + [s for s in ext if s not in universe]
                # Forward-scenario regimes use Yahoo for their historical analog
                # window — RTH-safe and free of IBKR contention. Always fetch
                # them regardless of the global `fetch` flag (which controls
                # the IBKR path).
                _, _, is_analog = reg.effective_window()
                if fetch or is_analog:
                    mw_data.ensure_market_data(reg, eff_universe)
                market = mw_data.load_market(reg, eff_universe)
                if not market:
                    log.warning("marswalk_no_data", regime=reg.id)
                    _state["done"] += 1
                    continue
                # Synthetic exchange-blackout transform (blackout_3day etc.).
                # Drops halt-window bars + gaps the first post-halt bar.
                if reg.halts:
                    market = apply_halts(market, reg.halts,
                                         gap_open_pct=reg.gap_open_pct or 0.0)
                # Synthetic shock transform (stacked_2x etc.) — single-day
                # permanent equity gap with no halt period.
                if reg.shocks:
                    market = apply_shocks(market, reg.shocks)
                # Price scaler for old regimes (stagflation_70s) so nominal $
                # levels match the $4M NLV scale. Returns unchanged.
                if reg.price_multiplier and reg.price_multiplier != 1.0:
                    mult = float(reg.price_multiplier)
                    market = {
                        sym: [(d, c * mult, iv) for (d, c, iv) in bars]
                        for sym, bars in market.items()
                    }
                earnings = mw_data.load_earnings(eff_universe)
                res = run_regime(reg.id, reg.name, reg.category, reg.rank,
                                 eff_universe, market, params, earnings=earnings,
                                 cash_yield_annual=reg.cash_yield_annual)
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
