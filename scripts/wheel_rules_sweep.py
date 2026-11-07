"""Wheel-rules sweep (2026-11-04) — the five Phase-1 changes, one factor at a time, then combined.

Rules (see src/strategy/wheel_rules.py, src/strategy/realized_move.py):
  1 stop     wheel_stop_loss_enabled / wheel_stop_loss_pct      {off, 0.06, 0.08, 0.10}
  2 unwind   cc_package_unwind_enabled                           {off, on}
  3 floor    put_otm_floor_mode / put_otm_realized_k             {flat, realized k 0.8 / 1.0 / 1.2}
  4 breach   put_universe_max_breach_rate                        {1.0 (off), 0.40, 0.30}
  5 cap      max_single_name_notional_pct (+ one-lot ceiling)    {0.25 (sim default), 0.10 strict, 0.10 w/ one lot ≤ 20%}
  B combined the proposed live set: stop 0.08 + unwind + realized k1.0 + breach 0.30 + cap 0.10

Regimes: the 6-year replay (decisive), the real account's own window (extended to 2026-11-03,
regime id live_acct_2026b, Yahoo backfill, 220k capital, current USD universe) and a crash set.
Rules 3/4 need pre-regime warm-up bars (regime "<id>_pre" in marswalk.db); regimes without
them fall back to the flat window / open gate exactly as live does on missing history —
the table prints `warm` so you can see which rows actually exercised them.

margin_interest_annual=0.055 so the carry of held stock is real in every cell.

Usage:
    .venv/bin/python scripts/wheel_rules_sweep.py            # full grid
    .venv/bin/python scripts/wheel_rules_sweep.py --baseline-only   # parity check row
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from marswalk.data import (fetch_spy_yahoo, fetch_symbols_yahoo, fetch_vix_yahoo,  # noqa: E402
                           load_earnings, load_market)
from marswalk.engine import Params, run_regime  # noqa: E402
from marswalk.models import get_mw_db, MarketBar  # noqa: E402
from marswalk.regimes import Regime, load_config  # noqa: E402

REPLAY = "live_replay_2020"
LIVE = "live_acct_2026b"
LIVE_REGIME = Regime(
    id=LIVE, rank=99, name="Live options account 2026-06-11 → 2026-11-03 (current USD universe)",
    category="lifecycle", start="2026-06-11", end="2026-11-03",
    why="The real account's own window; actual result −6.6% (EUR 205k / 220k).",
)
LIVE_CAPITAL = 220_000.0
REGIMES = [REPLAY, LIVE, "bear_2022", "q4_2018", "gfc_2008", "volmageddon_2018",
           "covid_2020", "bull_2021"]

_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True,
           "dte_min": 0, "dte_max": 3, "bull_regime_dte_min": 0, "bull_regime_dte_max": 3,
           "margin_interest_annual": 0.055}

CONFIGS: dict[str, dict] = {"baseline": {}}
for pct in (0.06, 0.08, 0.10):
    CONFIGS[f"stop_{pct:.2f}"] = {"wheel_stop_loss_enabled": True, "wheel_stop_loss_pct": pct}
CONFIGS["unwind"] = {"cc_package_unwind_enabled": True}
for k in (0.8, 1.0, 1.2):
    CONFIGS[f"floor_k{k}"] = {"put_otm_floor_mode": "realized", "put_otm_realized_k": k}
for b in (0.40, 0.30):
    CONFIGS[f"breach_{b:.2f}"] = {"put_universe_max_breach_rate": b}
CONFIGS["cap_0.10_strict"] = {"max_single_name_notional_pct": 0.10, "max_single_name_one_lot_pct": 0.10}
CONFIGS["cap_0.10_lot0.20"] = {"max_single_name_notional_pct": 0.10, "max_single_name_one_lot_pct": 0.20}
CONFIGS["COMBINED"] = {"wheel_stop_loss_enabled": True, "wheel_stop_loss_pct": 0.08,
                       "cc_package_unwind_enabled": True,
                       "put_otm_floor_mode": "realized", "put_otm_realized_k": 1.0,
                       "put_universe_max_breach_rate": 0.30,
                       "max_single_name_notional_pct": 0.10, "max_single_name_one_lot_pct": 0.20}
CONFIGS["COMBINED_no_breach"] = {k: v for k, v in CONFIGS["COMBINED"].items()
                                 if k != "put_universe_max_breach_rate"}


def _as_date(v):
    return v if isinstance(v, date) else datetime.strptime(str(v), "%Y-%m-%d").date()


def _annualized(ret_pct: float, days: int) -> float:
    return ((1 + ret_pct / 100.0) ** (365.0 / max(days, 1)) - 1) * 100.0 if days > 0 else 0.0


def _warm_count(regime_id: str) -> int:
    with get_mw_db() as db:
        return db.query(MarketBar.symbol).filter(MarketBar.regime_id == f"{regime_id}_pre") \
            .distinct().count()


def run_one(reg, universe, earnings, overrides: dict, start_capital=None) -> dict:
    market = load_market(reg, universe)
    if not market:
        return {"final_return_pct": None, "error": "no_data"}
    if getattr(reg, "halts", None):
        from marswalk.synthetic import apply_halts
        market = apply_halts(market, reg.halts, gap_open_pct=getattr(reg, "gap_open_pct", 0.0) or 0.0)
    if getattr(reg, "shocks", None):
        from marswalk.synthetic import apply_shocks
        market = apply_shocks(market, reg.shocks)
    pm = getattr(reg, "price_multiplier", None)
    if pm and pm != 1.0:
        market = {s: [(d, c * pm, iv) for (d, c, iv) in bars] for s, bars in market.items()}
    ov = {**_CRISIS, **overrides}
    if start_capital:
        ov["start_capital"] = start_capital
    res = run_regime(reg.id, reg.name, reg.category, reg.rank, universe, market, Params(**ov),
                     earnings=earnings, cash_yield_annual=getattr(reg, "cash_yield_annual", None))
    if not res:
        return {"final_return_pct": None, "error": "engine_returned_none"}
    nps = res.get("n_puts_sold", 0)
    days = (_as_date(reg.end) - _as_date(reg.start)).days
    return {
        "final_return_pct": res["final_return_pct"],
        "annualized_pct": _annualized(res["final_return_pct"], days),
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_trades": res["n_trades"], "n_assignments": res["n_assignments"],
        "n_puts_sold": nps, "put_assign_rate": (res["n_assignments"] / nps) if nps else None,
        "n_stop_losses": res.get("n_stop_losses", 0), "n_cc_unwinds": res.get("n_cc_unwinds", 0),
        "n_breach_skips": res.get("n_breach_skips", 0),
        "margin_interest_total": res.get("margin_interest_total", 0.0),
        "n_index_puts": res.get("n_index_puts", 0), "n_index_itm": res.get("n_index_itm", 0),
        "n_index_stops": res.get("n_index_stops", 0),
        "index_premium_sum": res.get("index_premium_sum", 0.0),
        "index_intrinsic_sum": res.get("index_intrinsic_sum", 0.0),
        "index_commission_sum": res.get("index_commission_sum", 0.0),
        "index_wing_cost_sum": res.get("index_wing_cost_sum", 0.0),
        "index_wing_payoff_sum": res.get("index_wing_payoff_sum", 0.0),
    }


_CACHE: dict = {}


def _job(spec):
    rid, cname = spec
    if "cfg" not in _CACHE:
        base_universe, regimes = load_config()
        live_universe = [s["symbol"] for s in
                         yaml.safe_load(open("config/options_universe.yaml"))["stocks"]
                         if s.get("currency") == "USD"]
        _CACHE["cfg"] = (base_universe, {r.id: r for r in regimes}, live_universe)
        _CACHE["earn"] = {}
    base_universe, by_id, live_universe = _CACHE["cfg"]
    if rid == LIVE:
        reg, uni, cap = LIVE_REGIME, live_universe, LIVE_CAPITAL
    else:
        reg, uni, cap = by_id[rid], base_universe, None
    key = "live" if rid == LIVE else "base"
    if key not in _CACHE["earn"]:
        _CACHE["earn"][key] = load_earnings(uni)
    r = run_one(reg, uni, _CACHE["earn"][key], CONFIGS[cname], start_capital=cap)
    return {"regime": rid, "config": cname, **r}


def main():
    baseline_only = "--baseline-only" in sys.argv
    out_path = Path("data") / f"wheel_rules_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base_universe, _ = load_config()
    live_universe = [s["symbol"] for s in yaml.safe_load(open("config/options_universe.yaml"))["stocks"]
                     if s.get("currency") == "USD"]
    print("preparing live-account regime (Yahoo backfill, idempotent)…", flush=True)
    try:
        fetch_symbols_yahoo(LIVE_REGIME, live_universe)
        fetch_vix_yahoo(LIVE_REGIME)
        fetch_spy_yahoo(LIVE_REGIME)
    except Exception as e:
        print(f"  live regime backfill failed: {e} — its rows will show as no_data")

    names = ["baseline"] if baseline_only else list(CONFIGS)
    jobs = [(rid, c) for rid in REGIMES for c in names]
    jobs.sort(key=lambda j: 0 if j[0] == REPLAY else 1)
    print(f"\nWheel-rules sweep — {len(jobs)} runs across {os.cpu_count()} cores")
    rows = []
    with ProcessPoolExecutor(max_workers=min(16, os.cpu_count() or 8)) as ex:
        futs = {ex.submit(_job, j): j for j in jobs}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:
                print(f"  JOB FAILED {futs[fut]}: {e}")
                continue
            rows.append(r)
            with out_path.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            if n % 10 == 0:
                print(f"  …{n}/{len(jobs)}", flush=True)
    _report(rows)
    print(f"\nwrote {out_path}")


def _report(rows):
    warm = {rid: _warm_count(rid) for rid in REGIMES}
    for rid in REGIMES:
        rr = [r for r in rows if r["regime"] == rid and r.get("final_return_pct") is not None]
        if not rr:
            continue
        base = next((r for r in rr if r["config"] == "baseline"), None)
        print(f"\n  {rid}   (warm-up names for rules 3/4: {warm.get(rid, 0)})")
        print(f"    {'config':20} {'return':>9} {'ann%':>7} {'maxDD':>6} {'puts':>5} {'asgn':>5} "
              f"{'asg%':>5} {'stops':>5} {'unw':>4} {'skips':>5} {'Δ vs base':>10}")
        for r in sorted(rr, key=lambda x: list(CONFIGS).index(x["config"])):
            d = (r["final_return_pct"] - base["final_return_pct"]) if base else 0.0
            ar = r["put_assign_rate"]
            print(f"    {r['config']:20} {r['final_return_pct']:>+8.2f}% {r['annualized_pct']:>+6.1f} "
                  f"{r['max_drawdown_pct']:>5.1f}% {r['n_puts_sold']:>5d} {r['n_assignments']:>5d} "
                  f"{(100 * ar if ar else 0):>4.0f}% {r['n_stop_losses']:>5d} {r['n_cc_unwinds']:>4d} "
                  f"{r['n_breach_skips']:>5d} {d:>+9.2f}pp")
    print("\n  Read the replay (noise floor ~10pp) and the live-account window first; discrete crash")
    print("  windows flatter patience (they are cut to include the recovery).")


if __name__ == "__main__":
    main()
