"""Below-breakeven CC A/B (2026-07-25) — strict last-resort dead-capital lever.

Rain's live account has assigned bags (LRCX/IREN) that get NO covered call
because live refuses to write below breakeven and a >=breakeven far-OTM strike
has no fee-clearing bid — so ~$50k sits dead. The below-breakeven lever writes a
CAPPED below-breakeven OTM call, but ONLY when a lot is aged (>=90d) + deep
(>=20% below basis) + in a confirmed downtrend (below MA200, no 20d momentum),
and never locks more than 10% below basis, with the strike above spot so a loss
is realized only on a recovery — never a fire-sale at the bottom.

An UNGATED version was rejected in-sim before (cc-multiassignment deep-dump).
This tests whether the strict gates change the verdict — vs current (lever OFF).

HONEST LIMIT: MarsWalk has NO fee floor, so it ALWAYS finds a coverable CC even
at >=breakeven — meaning the live problem (no bid at breakeven) barely exists in
sim, so the lever fires far less here than it would matter live. This sweep
therefore tests the PRICE-PATH question: does disciplined below-breakeven capping
help or hurt the equity curve over 6y? The live income-unlock upside is on top of
whatever this shows and is not captured here.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/cc_below_breakeven_sweep.py
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
_BASE = {**_CRISIS, "dte_min": 0, "dte_max": 3}
# Confirmed gates: 90d age, 20% depth, 10% max lock, downtrend required (engine default).
CONFIGS = {
    "baseline":  {**_BASE, "cc_below_breakeven_enabled": False},
    "bbe_strict": {**_BASE, "cc_below_breakeven_enabled": True,
                   "cc_bbe_min_age_days": 90, "cc_bbe_min_drawdown": 0.20, "cc_bbe_max_lock": 0.10},
    # sensitivity: a looser age (60d) and a tighter lock (5%) to bracket the choice
    "bbe_age60": {**_BASE, "cc_below_breakeven_enabled": True,
                  "cc_bbe_min_age_days": 60, "cc_bbe_min_drawdown": 0.20, "cc_bbe_max_lock": 0.10},
    "bbe_lock5": {**_BASE, "cc_below_breakeven_enabled": True,
                  "cc_bbe_min_age_days": 90, "cc_bbe_min_drawdown": 0.20, "cc_bbe_max_lock": 0.05},
}
BASELINE = "baseline"
MIN_ANNUAL = 15.5


def _as_date(v):
    return v if isinstance(v, date) else datetime.strptime(str(v), "%Y-%m-%d").date()


def _annualized(r, days):
    return ((1 + r / 100.0) ** (365.0 / days) - 1) * 100.0 if days > 0 else 0.0


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
        "n_bbe_writes": res.get("n_bbe_writes", 0),
    }


def _cell(r):
    if r["final_return_pct"] is None:
        return "ERR".rjust(20)
    return f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.1f}/{r['n_bbe_writes']}".rjust(20)


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)
    out = Path("data") / f"cc_below_breakeven_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out.unlink(missing_ok=True)

    names = list(CONFIGS.keys())
    print()
    print("Below-breakeven CC sweep — lever OFF (baseline) vs strict-gated ON. cell = ret/maxDD/n_bbe_writes")
    print("=" * 118)
    print("  " + "regime".ljust(16) + "cat".ljust(8) + "  ".join(n.rjust(20) for n in names))
    print("  " + "-" * 104)
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
            cols.append(_cell(r))
        print("  " + rid.ljust(16) + cat.ljust(8) + "  ".join(cols))

    rep = results.get(REPLAY, {}); base = rep.get(BASELINE)
    print()
    print("=" * 118)
    print("VERDICT — live_replay_2020 (strict rule: 90d age, 20% deep, 10% max lock, downtrend required)")
    print("=" * 118)
    if base and base["final_return_pct"] is not None:
        print(f"  {'config'.ljust(12)} {'annual%':>8} {'total%':>9} {'maxDD':>6} {'n_asgn':>7} "
              f"{'uncov_days':>11} {'bbe_writes':>11} {'Δann':>7}")
        for name in CONFIGS:
            r = rep.get(name)
            if not r or r["final_return_pct"] is None:
                continue
            dann = r["annualized_pct"] - base["annualized_pct"]
            print(f"  {name.ljust(12)} {r['annualized_pct']:>7.1f}% {r['final_return_pct']:>+8.1f}% "
                  f"{r['max_drawdown_pct']:>5.1f}% {r['n_assignments']:>7d} {r['uncovered_lot_days']:>11d} "
                  f"{r['n_bbe_writes']:>11d} {dann:>+6.2f}")
        print()
        print("  Interpret: n_bbe_writes ~0 on the replay => the fee-floor-free sim rarely needs it")
        print("  (it always covers at >=breakeven), so Δann ~0 is EXPECTED and does NOT argue against")
        print("  the LIVE lever, whose whole value is unlocking income where live gets no bid. Watch")
        print("  the CRASH rows + uncovered_lot_days for the price-path effect where it DOES fire.")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
