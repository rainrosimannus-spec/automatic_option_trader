"""Covered-call coverage-LAG sweep (2026-07-25) — the "same-cycle CC" idea.

Rain's live account shows the wheel collecting premium (~$32k) but 7 assigned
bags at -$33.5k unrealized, several UNCOVERED for weeks (LRCX 23d, IREN 27d) —
which is why the live account doesn't look like the 15.5%/yr backtest. This
sweep quantifies what an assigned lot sitting UNCOVERED costs, by injecting a
coverage lag (cc_coverage_lag_days) into the engine: lag=0 is same-cycle (write
the CC the day of assignment, the engine's default best case); lag=N models the
lot sitting uncovered N trading days before its first CC.

Reports the usual return/DD plus uncovered_lot_days (Σ over days of held
100-share lots with no covering CC) — the dead-capital proxy.

HONEST LIMITATION (read before trusting): MarsWalk ALWAYS finds a coverable CC
because it fills at bid with NO fee floor and NO below-breakeven constraint
(engine.py ~996). So it CANNOT represent the real trap — a deeply underwater lot
(LRCX/IREN) where every >=breakeven strike has no fee-clearing bid, so NO CC is
writable at all. This sweep therefore measures only the cost of DELAY on lots
that are coverable; it does NOT capture the deep-underwater dead-capital that is
actually hurting the live account. That drag is a fee-floor + breakeven-rule
phenomenon MarsWalk is structurally blind to.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/cc_coverage_lag_sweep.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from marswalk.data import load_earnings, load_market
from marswalk.engine import Params, run_regime
from marswalk.regimes import load_config

BENIGN = ["bull_2021", "grind_2024h1", "chop_2023h2", "iran_war_2026"]
CRASH = ["covid_2020", "svb_2023", "q4_2018", "bear_2022", "gfc_2008", "stacked_2x", "blackout_3day"]
REPLAY = "live_replay_2020"
TARGETS = BENIGN + CRASH

_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True}
LAGS = [0, 1, 2, 3, 5, 10]        # 0 = same-cycle (current default best case)
CONFIGS = {f"lag_{k}": {**_CRISIS, "dte_min": 0, "dte_max": 3, "cc_coverage_lag_days": k} for k in LAGS}
BASELINE = "lag_0"


def _as_date(v):
    return v if isinstance(v, date) else datetime.strptime(str(v), "%Y-%m-%d").date()


def _annualized(final_return_pct, days):
    return ((1 + final_return_pct / 100.0) ** (365.0 / days) - 1) * 100.0 if days > 0 else 0.0


def run_one(reg, universe, earnings, overrides):
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
        market = {sym: [(d, c * pm, iv) for (d, c, iv) in bars] for sym, bars in market.items()}
    res = run_regime(reg.id, reg.name, reg.category, reg.rank, universe, market,
                     Params(**overrides), earnings=earnings,
                     cash_yield_annual=getattr(reg, "cash_yield_annual", None))
    if not res:
        return {"final_return_pct": None, "error": "engine_returned_none"}
    days = (_as_date(reg.end) - _as_date(reg.start)).days
    return {
        "final_return_pct": res["final_return_pct"],
        "annualized_pct": _annualized(res["final_return_pct"], days),
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_assignments": res["n_assignments"],
        "uncovered_lot_days": res.get("uncovered_lot_days", 0),
    }


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)
    out = Path("data") / f"cc_coverage_lag_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out.unlink(missing_ok=True)

    names = list(CONFIGS.keys())
    print()
    print(f"CC coverage-lag sweep — same-cycle (lag_0) vs delayed. cell = ann%/maxDD/uncov_lot_days")
    print("=" * 122)
    print("  " + "regime".ljust(16) + "cat".ljust(8) + "  ".join(n.rjust(16) for n in names))
    print("  " + "-" * 112)
    results = {}
    for rid in TARGETS + [REPLAY]:
        reg = by_id.get(rid)
        if not reg:
            print(f"  {rid} MISSING"); continue
        cat = "REPLAY" if rid == REPLAY else ("CRASH" if rid in CRASH else "benign")
        results[rid] = {}; cols = []
        for name, ov in CONFIGS.items():
            r = run_one(reg, universe, earnings, ov)
            results[rid][name] = r
            with out.open("a") as fh:
                fh.write(json.dumps({"regime": rid, "config": name, **r}) + "\n")
            if r["final_return_pct"] is None:
                cols.append("ERR".rjust(16))
            else:
                cols.append(f"{r['annualized_pct']:+.1f}/{r['max_drawdown_pct']:.1f}/{r['uncovered_lot_days']}".rjust(16))
        print("  " + rid.ljust(16) + cat.ljust(8) + "  ".join(cols))

    rep = results.get(REPLAY, {})
    base = rep.get(BASELINE)
    print()
    print("=" * 122)
    print("VERDICT — live_replay_2020: does same-cycle (lag_0) beat delayed coverage?")
    print("=" * 122)
    if base and base["final_return_pct"] is not None:
        print(f"  {'lag(days)':>9} {'annual%':>8} {'total%':>9} {'maxDD':>6} {'n_asgn':>7} {'uncov_lot_days':>15} {'Δann pp':>9}")
        for name in CONFIGS:
            r = rep.get(name)
            if not r or r["final_return_pct"] is None:
                continue
            dann = r["annualized_pct"] - base["annualized_pct"]
            print(f"  {name.split('_')[1]:>9} {r['annualized_pct']:>7.1f}% {r['final_return_pct']:>+8.1f}% "
                  f"{r['max_drawdown_pct']:>5.1f}% {r['n_assignments']:>7d} {r['uncovered_lot_days']:>15d} {dann:>+8.2f}")
        print()
        print("  Read: if Δann is ~0 across lags, coverage TIMING barely matters in-sim — the live")
        print("  drag is the deep-underwater/fee-floor trap MarsWalk can't model, not slow coverage.")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
