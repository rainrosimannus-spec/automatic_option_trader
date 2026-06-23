"""Regime-specific covered-call A/B sweep (2026-06-23).

Validates the velocity-always / crash-bolster CC redesign. Three configs:

  - baseline  : Params() defaults — run on `git stash`ed OLD engine for the
                pre-redesign patient/distressed comparison. (mode --baseline)
  - velo_only : crash detector OFF → velocity-always in EVERY regime (no
                bolster). The "just dump fast everywhere" counterfactual.
  - regime    : crash detector ON → velocity normal + bolster in crashes (the
                shipped design).

Decisive question (per plan): does the bolster branch make more / lose less in
crash regimes than velocity-everywhere, with NO benign-regime regression?

READ-ONLY against the live trading engine — only writes a results JSONL.

Usage:
    source .venv/bin/activate
    # 1) new-code A/B (current working tree):
    PYTHONPATH=src python3 scripts/cc_regime_sweep.py
    # 2) old baseline (after `git stash` of the tracked edits):
    PYTHONPATH=src python3 scripts/cc_regime_sweep.py --baseline
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

# --baseline runs the OLD engine (Params with no new fields) → defaults only.
BASELINE_MODE = "--baseline" in sys.argv
if BASELINE_MODE:
    CONFIGS = {"baseline": {}}
else:
    # velo_only = bolster OFF (velocity-always everywhere). The 3 b_* configs are
    # candidate crash branches (plan step: sweep picks the winner). Floor handling
    # is fixed in code (crash=strict); these vary the crash delta band + DTE:
    #   b_patient = leading hypothesis (defensive OTM 0.15-0.30, 21 DTE)
    #   b_deepITM = deep-ITM in crash (0.80-0.95, 7 DTE) — needs strike≥basis
    #   b_wideOTM = far-OTM cushion harvest (0.08-0.18, 30 DTE)
    # Mirror the LIVE crisis config: both detectors on → STRANGLE action (the
    # actual live crisis tool). Compare CC behavior WITH the strangle present.
    #   no_strangle  = CC velocity only, strangle off (isolates the CC change)
    #   live_crisis  = full live: velocity CC + detector-triggered strangle
    # Run this script on NEW engine, then `git stash push src/marswalk/engine.py`
    # and run again for OLD-CC under the SAME configs.
    _crisis = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
               "crash_when_active_enabled": True, "crash_strangle_when_active": True}
    CONFIGS = {
        "no_strangle": {"crash_when_active_enabled": True},
        "live_crisis": {**_crisis},
    }


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

    tag = "baseline" if BASELINE_MODE else "ab"
    out_path = Path("data") / f"cc_regime_sweep_{tag}_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    cfg_names = list(CONFIGS.keys())
    print()
    print(f"CC regime sweep [{tag}] — {len(TARGETS)} regimes × {len(cfg_names)} configs")
    print("=" * 96)
    hdr = "regime".ljust(16) + "cat".ljust(7) + "  ".join(f"{c}:ret%/maxDD/asgn/crD".rjust(28) for c in cfg_names)
    print("  " + hdr)
    print("  " + "-" * (len(hdr)))

    results = {}
    for rid in TARGETS:
        reg = by_id.get(rid)
        if not reg:
            print(f"  {rid.ljust(16)} MISSING")
            continue
        cat = "CRASH" if rid in CRASH else "benign"
        results[rid] = {}
        cols = []
        for cfg_name, ov in CONFIGS.items():
            r = run_one(reg, universe, earnings, ov)
            results[rid][cfg_name] = r
            with out_path.open("a") as fh:
                fh.write(json.dumps({"regime": rid, "config": cfg_name, **r}) + "\n")
            if r["final_return_pct"] is None:
                cols.append("ERR".rjust(28))
            else:
                cols.append(f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.1f}/{r['n_assignments']}/{r['n_crash_days']}".rjust(28))
        print("  " + rid.ljust(16) + cat.ljust(7) + "  ".join(cols))

    # Bolster value = each b_* − velo_only (only meaningful in non-baseline mode).
    if not BASELINE_MODE:
        cand_names = [c for c in cfg_names if c != "no_strangle"]
        print()
        print("=" * 96)
        print("Bolster value = candidate − velo_only  (Δreturn pp).  Want: >0 in CRASH, =0 in benign")
        print("=" * 96)
        crash_totals = {c: 0.0 for c in cand_names}
        for grp_name, grp in (("CRASH", CRASH), ("BENIGN", BENIGN)):
            print(f"  -- {grp_name} --")
            for rid in grp:
                base = results.get(rid, {}).get("no_strangle")
                if not base or base["final_return_pct"] is None:
                    print(f"    {rid.ljust(16)} n/a"); continue
                row = [f"crD={results[rid][cand_names[0]]['n_crash_days']}".rjust(8)]
                for c in cand_names:
                    a = results.get(rid, {}).get(c)
                    if not a or a["final_return_pct"] is None:
                        row.append(f"{c}=ERR".rjust(16)); continue
                    d = a["final_return_pct"] - base["final_return_pct"]
                    if grp_name == "CRASH":
                        crash_totals[c] += d
                    row.append(f"{c}={d:+.2f}".rjust(16))
                print(f"    {rid.ljust(16)}" + "  ".join(row))
        print("\n  CRASH-sum Δret vs velo_only:  " + "  ".join(f"{c}={crash_totals[c]:+.2f}pp" for c in cand_names))
        print("  (winner = highest CRASH-sum with no benign regression)")

    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
