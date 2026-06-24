"""Rescue-threshold sweep (2026-06-24).

The rescue line `in_rescue = spot < cb * cc_rescue_threshold` is the velocity-vs-
hold lever for *mildly* underwater assigned lots:

  spot >= thr*cb (velocity)  -> attempt deep-ITM dump; floor-blocked names fall
                                back to the 0.15-0.35 band (closer to money ->
                                called away near the depressed price -> realizes
                                the small loss faster).
  spot <  thr*cb (rescue)    -> skip deep-ITM, use the deeper 0.05-0.35 OTM band
                                (more cushion, holds longer, less likely to
                                realize).

Higher thr (->1.0) = bail to cushion sooner; lower thr (->0.90) = keep trying to
dump further underwater. Decisive question: does any threshold beat the live 0.95
in CRASH regimes WITHOUT a benign regression?

Fixed base = LIVE crisis config (strangle on) so the CC change is judged with the
real crisis tool present. READ-ONLY against the live engine — only writes JSONL.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/cc_rescue_threshold_sweep.py
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

BENIGN = ["bull_2021", "grind_2024h1", "chop_2023h2", "iran_war_2026"]
CRASH = ["covid_2020", "svb_2023", "q4_2018", "bear_2022", "gfc_2008", "stacked_2x", "blackout_3day"]
TARGETS = BENIGN + CRASH

# Live crisis config (strangle on) — fixed; vary only cc_rescue_threshold.
_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True}
THRESHOLDS = [0.90, 0.93, 0.95, 0.97, 1.00]   # 0.95 = live baseline
CONFIGS = {f"thr_{t:.2f}": {**_CRISIS, "cc_rescue_threshold": t} for t in THRESHOLDS}
BASELINE = "thr_0.95"


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
    return {
        "final_return_pct": res["final_return_pct"],
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_trades": res["n_trades"],
        "n_assignments": res["n_assignments"],
        "n_crash_days": res.get("n_crash_days", 0),
    }


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)

    out_path = Path("data") / f"cc_rescue_thr_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    cfg_names = list(CONFIGS.keys())
    print()
    print(f"Rescue-threshold sweep — {len(TARGETS)} regimes × {len(cfg_names)} thresholds (base={BASELINE})")
    print("=" * 110)
    hdr = "regime".ljust(16) + "cat".ljust(7) + "  ".join(f"{c}:ret/DD".rjust(18) for c in cfg_names)
    print("  " + hdr)
    print("  " + "-" * len(hdr))

    results = {}
    for rid in TARGETS:
        reg = by_id.get(rid)
        if not reg:
            print(f"  {rid.ljust(16)} MISSING"); continue
        cat = "CRASH" if rid in CRASH else "benign"
        results[rid] = {}
        cols = []
        for cfg_name, ov in CONFIGS.items():
            r = run_one(reg, universe, earnings, ov)
            results[rid][cfg_name] = r
            with out_path.open("a") as fh:
                fh.write(json.dumps({"regime": rid, "config": cfg_name, **r}) + "\n")
            if r["final_return_pct"] is None:
                cols.append("ERR".rjust(18))
            else:
                cols.append(f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.1f}".rjust(18))
        print("  " + rid.ljust(16) + cat.ljust(7) + "  ".join(cols))

    # Δ vs baseline (thr_0.95), summed per group.
    print()
    print("=" * 110)
    print(f"Δreturn pp vs {BASELINE}  (want: >0 in CRASH, ~0 in benign).  [Δret / ΔmaxDD]")
    print("=" * 110)
    cand = [c for c in cfg_names if c != BASELINE]
    for grp_name, grp in (("CRASH", CRASH), ("BENIGN", BENIGN)):
        sums = {c: 0.0 for c in cand}
        dd_sums = {c: 0.0 for c in cand}
        print(f"  -- {grp_name} --")
        for rid in grp:
            base = results.get(rid, {}).get(BASELINE)
            if not base or base["final_return_pct"] is None:
                print(f"    {rid.ljust(16)} n/a"); continue
            row = []
            for c in cand:
                a = results[rid].get(c)
                if not a or a["final_return_pct"] is None:
                    row.append(f"{c}=ERR".rjust(20)); continue
                dret = a["final_return_pct"] - base["final_return_pct"]
                ddd = a["max_drawdown_pct"] - base["max_drawdown_pct"]
                sums[c] += dret; dd_sums[c] += ddd
                row.append(f"{c}={dret:+.2f}/{ddd:+.1f}".rjust(20))
            print(f"    {rid.ljust(16)}" + "  ".join(row))
        print(f"  {grp_name}-sum Δret:  " + "  ".join(f"{c}={sums[c]:+.2f}" for c in cand))
        print(f"  {grp_name}-sum ΔDD :  " + "  ".join(f"{c}={dd_sums[c]:+.1f}" for c in cand))
        print()

    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
