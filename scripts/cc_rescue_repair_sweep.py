"""Rescue DTE-repair A/B sweep (2026-06-28).

The legacy rescue band wrote a 0.05-0.35Δ call at 1-7 DTE above the breakeven
floor — which LIVE finds ~zero economic premium (a >10% OTM call that short has
delta <0.05 / premium under the fee floor → no candidate → naked lot). The repair
relaxes only the DTE (1-7 → 30-60), same delta band, same floor, so an above-
breakeven call carries real time-value and never locks a loss.

  baseline "repair_off" = cc_rescue_repair_enabled=False (legacy 1-7 DTE rescue).
  variants  "repair_m{30,45,60}" = repair ON, cc_rescue_repair_dte_max swept.

⚠ READ THE CAVEAT: MarsWalk prices the chain with Black-Scholes and fills at bid
with NO commission/fee floor, so in the SIM the legacy 1-7 DTE band ALWAYS finds
a cushion call — the very live failure this fixes does not exist in the sim. So a
sim "drag" here is NOT a ship/no-ship signal for the repair; it only bounds the
opportunity-cost side (longer DTE caps the recovery bounce). Shipped on live
reasoning; this sweep is for the downside bound + future live A/B.

Fixed base = LIVE crisis config (strangle on). READ-ONLY — only writes JSONL.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/cc_rescue_repair_sweep.py
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
REPLAY = "live_replay_2020"

_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True}

CONFIGS = {
    "repair_off": {**_CRISIS, "cc_rescue_repair_enabled": False},                       # baseline: legacy 1-7 DTE
    "repair_m30": {**_CRISIS, "cc_rescue_repair_enabled": True, "cc_rescue_repair_dte_min": 30, "cc_rescue_repair_dte_max": 30},
    "repair_m45": {**_CRISIS, "cc_rescue_repair_enabled": True, "cc_rescue_repair_dte_min": 30, "cc_rescue_repair_dte_max": 45},
    "repair_m60": {**_CRISIS, "cc_rescue_repair_enabled": True, "cc_rescue_repair_dte_min": 30, "cc_rescue_repair_dte_max": 60},  # live default
}
BASELINE = "repair_off"


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
    nt, na = res["n_trades"], res["n_assignments"]
    return {
        "final_return_pct": res["final_return_pct"],
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_trades": nt,
        "n_assignments": na,
        "assignment_rate": round(na / nt, 4) if nt else None,
        "n_crash_days": res.get("n_crash_days", 0),
    }


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)

    out_path = Path("data") / f"cc_rescue_repair_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    cfg_names = list(CONFIGS.keys())
    print()
    print(f"Rescue DTE-repair sweep — {len(TARGETS)} regimes × {len(cfg_names)} configs (base={BASELINE})")
    print("(sim has no fee floor → it can't see the repair's live upside; drag here = downside bound only)")
    print("=" * 110)
    hdr = "regime".ljust(16) + "cat".ljust(7) + "  ".join(c.rjust(14) for c in cfg_names)
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
            cols.append("ERR".rjust(14) if r["final_return_pct"] is None
                        else f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.0f}".rjust(14))
        print("  " + rid.ljust(16) + cat.ljust(7) + "  ".join(cols))

    print()
    print("=" * 110)
    print(f"Δreturn pp vs {BASELINE}  [Δret / ΔmaxDD]")
    print("=" * 110)
    cand = [c for c in cfg_names if c != BASELINE]
    for grp_name, grp in (("CRASH", CRASH), ("BENIGN", BENIGN)):
        sums = {c: 0.0 for c in cand}
        dd_sums = {c: 0.0 for c in cand}
        for rid in grp:
            base = results.get(rid, {}).get(BASELINE)
            if not base or base["final_return_pct"] is None:
                continue
            for c in cand:
                a = results[rid].get(c)
                if not a or a["final_return_pct"] is None:
                    continue
                sums[c] += a["final_return_pct"] - base["final_return_pct"]
                dd_sums[c] += a["max_drawdown_pct"] - base["max_drawdown_pct"]
        print(f"  -- {grp_name} --")
        print(f"  sum Δret:  " + "  ".join(f"{c}={sums[c]:+.2f}" for c in cand))
        print(f"  sum ΔDD :  " + "  ".join(f"{c}={dd_sums[c]:+.1f}" for c in cand))
        print()

    # ── live_replay_2020 (6-year continuous) — run last, streamed ──
    print("=" * 110)
    print(f"replay: {REPLAY}  (opportunity-cost bound; NOT the ship signal — see caveat)")
    print("=" * 110)
    rep = by_id.get(REPLAY)
    if not rep:
        print(f"  {REPLAY} not in regime config — skipped")
    else:
        base_rep = None
        for cfg_name, ov in CONFIGS.items():
            r = run_one(rep, universe, earnings, ov)
            with out_path.open("a") as fh:
                fh.write(json.dumps({"regime": REPLAY, "config": cfg_name, **r}) + "\n")
            if cfg_name == BASELINE:
                base_rep = r
            tag = "  (baseline)" if cfg_name == BASELINE else ""
            if r["final_return_pct"] is None:
                print(f"  {cfg_name.ljust(14)} ERR{tag}")
            else:
                dr = ""
                if base_rep and base_rep["final_return_pct"] is not None and cfg_name != BASELINE:
                    dr = f"   Δret={r['final_return_pct'] - base_rep['final_return_pct']:+.2f}pp  ΔDD={r['max_drawdown_pct'] - base_rep['max_drawdown_pct']:+.1f}"
                print(f"  {cfg_name.ljust(14)} ret={r['final_return_pct']:+.1f}%  DD={r['max_drawdown_pct']:.1f}%  "
                      f"asgn_rate={r['assignment_rate']}{dr}{tag}")

    print()
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
