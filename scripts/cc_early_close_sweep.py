"""Deep-ITM covered-call early-close sweep (2026-08-05).

Tests the automated version of Rain's manual move: when an assigned lot's stock
runs far above the covered-call strike, the call is CAPPED but not yet called —
it yields strike+premium regardless, yet the capital stays locked (on margin)
until expiry. Rule A buys-to-close the deep-ITM call + sells the stock early once
the remaining time value has decayed to ~0, banking the same P&L a few days early,
killing the margin carry, and freeing cash+slot to sell puts now.

Two levers, six arms:
  baseline   — current live (Rule A + B both off)
  ec_tight   — Rule A, fires often, minimal give-up (over=5%, ext<=0.5%)
  ec_mid     — Rule A, default candidate       (over=8%, ext<=1.0%)
  ec_wide    — Rule A, fires late, cheapest     (over=12%, ext<=2.0%)
  nocap_mom  — Rule B, don't cap momentum lots (hold uncapped for upside)
  combo      — ec_mid + nocap_mom

CRUCIAL: MarsWalk historically charged ZERO interest on margin debit, so Rule A's
carry benefit was invisible. Each arm is run at TWO margin rates —
  m0  = 0.0    (byte-comparable to every prior sweep; baseline|m0 must reproduce
               the recorded live_replay dte_3 numbers → proves the new params are
               inert when off)
  mON = 0.055  (IBKR-ish; makes the carry cost real so the A/B isn't rigged against
               the very thing Rule A fixes)
Rule A's true edge = ec_*|mON − baseline|mON. The m0 pass isolates the pure
price-path/velocity slice.

READ-ONLY against the live engine — only writes a results JSONL.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/cc_early_close_sweep.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from marswalk.data import load_earnings, load_market
from marswalk.engine import Params, run_regime
from marswalk.regimes import load_config

# Momentum / up-trend regimes are where lots rise above strikes (Rule A/B act);
# crash regimes are the drawdown safety check; replay is the decisive 6y witness.
BENIGN = ["bull_2021", "grind_2024h1", "ai_2023", "iran_war_2026"]
CRASH = ["covid_2020", "bear_2022", "q4_2018"]
REPLAY = "live_replay_2020"
TARGETS = BENIGN + CRASH

# Live crisis config (strangle on) — fixed across arms, mirrors the other sweeps.
_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True}

_EC = lambda over, ext: {"cc_early_close_enabled": True,
                         "cc_early_close_stock_over_strike_pct": over,
                         "cc_early_close_max_extrinsic_pct": ext}
ARMS = {
    "baseline":  {},
    "ec_tight":  _EC(0.05, 0.005),
    "ec_mid":    _EC(0.08, 0.010),
    "ec_wide":   _EC(0.12, 0.020),
    "nocap_mom": {"cc_skip_on_momentum": True},
    "combo":     {**_EC(0.08, 0.010), "cc_skip_on_momentum": True},
}
# Margin-interest is set on but comes out $0: the sim's cash never goes negative on
# the replay (start capital + premium covers all NLV-capped deployment), so MarsWalk
# structurally cannot model the margin-loan carry that is Rule A's whole economic
# point. Verified base_m0 == base_mON == +140.55% / margin_int=$0. So a single pass
# at a nonzero rate suffices; the return deltas are the PRICE-PATH/VELOCITY slice
# only, NOT the carry Rain actually captures in the real (margin-running) account.
MARGINS = {"mON": 0.055}
BASELINE = "baseline"


def run_one(reg, universe, earnings, overrides: dict) -> dict:
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
    p = Params(**overrides)
    res = run_regime(reg.id, reg.name, reg.category, reg.rank, universe, market, p,
                     earnings=earnings, cash_yield_annual=getattr(reg, "cash_yield_annual", None))
    if not res:
        return {"final_return_pct": None, "error": "engine_returned_none"}
    n = res["n_trades"]
    return {
        "final_return_pct": res["final_return_pct"],
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_trades": n,
        "n_assignments": res["n_assignments"],
        "n_cc_early_closes": res.get("n_cc_early_closes", 0),
        "margin_interest_total": res.get("margin_interest_total", 0.0),
        "final_nlv": res.get("final_nlv"),
        "assignment_rate": (res["n_assignments"] / n) if n else None,
    }


def _cell(r) -> str:
    if r is None or r["final_return_pct"] is None:
        return "ERR".rjust(22)
    return (f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.1f}"
            f"/ec{r['n_cc_early_closes']}").rjust(22)


def _delta_block(title, group, results, margin_key):
    cand = [c for c in ARMS if c != BASELINE]
    sums = {c: 0.0 for c in cand}
    dd_sums = {c: 0.0 for c in cand}
    print(f"  -- {title} ({margin_key}) --")
    for rid in group:
        base = results.get((rid, margin_key), {}).get(BASELINE)
        if not base or base["final_return_pct"] is None:
            print(f"    {rid.ljust(16)} n/a"); continue
        row = []
        for c in cand:
            a = results[(rid, margin_key)].get(c)
            if not a or a["final_return_pct"] is None:
                row.append(f"{c}=ERR".rjust(24)); continue
            dret = a["final_return_pct"] - base["final_return_pct"]
            ddd = a["max_drawdown_pct"] - base["max_drawdown_pct"]
            sums[c] += dret; dd_sums[c] += ddd
            row.append(f"{c}={dret:+.2f}/{ddd:+.1f}".rjust(24))
        print(f"    {rid.ljust(16)}" + "  ".join(row))
    if len(group) > 1:
        print(f"  {title}-sum Δret:  " + "  ".join(f"{c}={sums[c]:+.2f}" for c in cand))
        print(f"  {title}-sum ΔDD :  " + "  ".join(f"{c}={dd_sums[c]:+.1f}" for c in cand))
    print()


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)

    out_path = Path("data") / f"cc_early_close_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    print()
    print(f"CC early-close sweep — {len(TARGETS)}+1 regimes × {len(ARMS)} arms × {len(MARGINS)} margin; "
          f"cell = ret/maxDD/#earlyCloses")
    print("=" * 132)
    hdr = "regime".ljust(16) + "mgn".ljust(6) + "  ".join(c.rjust(22) for c in ARMS)
    print("  " + hdr)
    print("  " + "-" * len(hdr))

    results = {}
    for rid in TARGETS + [REPLAY]:
        reg = by_id.get(rid)
        if not reg:
            print(f"  {rid.ljust(16)} MISSING"); continue
        for mkey, mrate in MARGINS.items():
            results[(rid, mkey)] = {}
            cols = []
            for arm, ov in ARMS.items():
                overrides = {**_CRISIS, **ov, "margin_interest_annual": mrate}
                r = run_one(reg, universe, earnings, overrides)
                results[(rid, mkey)][arm] = r
                with out_path.open("a") as fh:
                    fh.write(json.dumps({"regime": rid, "margin": mkey, "arm": arm, **r}) + "\n")
                cols.append(_cell(r))
            print("  " + rid.ljust(16) + mkey.ljust(6) + "  ".join(cols))

    print()
    print("=" * 132)
    print(f"Δ vs {BASELINE} (same margin) [Δret pp / ΔmaxDD pp].  Rule A carry edge = read the mON block.")
    print("=" * 132)
    for mkey in MARGINS:
        _delta_block("REPLAY", [REPLAY], results, mkey)
        _delta_block("BENIGN", BENIGN, results, mkey)
        _delta_block("CRASH", CRASH, results, mkey)

    # Reproduction guard + carry-channel diagnostic.
    b = results.get((REPLAY, "mON"), {}).get(BASELINE)
    if b:
        print("-" * 132)
        print(f"REPRODUCTION CHECK — baseline live_replay: ret={b['final_return_pct']:+.2f}% "
              f"maxDD={b['max_drawdown_pct']:.2f}% n_trades={b['n_trades']} "
              f"n_assign={b['n_assignments']}  (must reproduce recorded dte_3 baseline)")
    total_mi = sum(r.get("margin_interest_total", 0.0) or 0.0
                   for cell in results.values() for r in cell.values() if r)
    print(f"CARRY-CHANNEL DIAGNOSTIC — Σ margin_interest across ALL runs = ${total_mi:,.0f}. "
          f"If ~0, the sim never ran a margin debit → Rule A's real carry benefit is INVISIBLE here; "
          f"the return deltas are price-path/velocity only.")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
