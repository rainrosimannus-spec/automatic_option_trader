"""Long-grind regime countermeasure sweep.

Runs all MarsWalk regimes under four configurations:
  - baseline (current default)
  - +bear_dte  (Candidate 1: bear-regime DTE extension 0-3 → 3-10)
  - +vix_k     (Candidate 2: VIX-adaptive short_dte_uplift_k)
  - +stag      (Candidate 3: stagnation-aware sizing booster)

Prints a delta grid and a per-candidate summary. Writes raw results to
data/longgrind_sweep_YYYYMMDD.jsonl for later analysis.

This is READ-ONLY against the live trading engine. It only mutates the
MarsWalk results JSONL output file.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/longgrind_sweep.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))   # marswalk.*
sys.path.insert(0, str(_ROOT))            # src.core.logger (used inside marswalk.data)

from marswalk.data import load_earnings, load_market
from marswalk.engine import Params, run_regime
from marswalk.regimes import load_config


CONFIGS: dict[str, dict] = {
    "baseline":   {},
    "+bear_dte":  {"bear_regime_enabled": True},
    "+vix_k":     {"k_vix_adaptive_enabled": True},
    "+stag":      {"stagnation_boost_enabled": True},
    # Multi-leg short structures (PROTOTYPE 2026-05-28). MarsWalk-only.
    "+strangle":  {"strangle_mode": "strangle"},
    "+iron_cdr":  {"strangle_mode": "iron_condor"},
}

# Regimes the user flagged as long-grind underperformers (the targets to lift).
LONG_GRIND_TARGETS = ["ai_crash", "oil_crash_2014", "stagflation_70s"]

# Regimes that should NOT be hurt — bull/grind regimes carrying the wheel.
BULL_TARGETS = ["bull_2021", "ai_2023", "grind_2024h1", "iran_war_2026"]


def run_one(reg, universe, earnings, overrides: dict) -> dict:
    """Run one regime + config. Applies all per-regime synthetic transforms
    (halts/shocks/price_multiplier) and threads cash_yield_annual to engine —
    so regimes added since the original sweep (blackout_3day / stacked_2x /
    stagflation_70s) behave correctly."""
    market = load_market(reg, universe)
    if not market:
        return {"final_return_pct": None, "error": "no_data"}
    # Apply synthetic transforms (mirrors src/marswalk/service.py)
    if getattr(reg, "halts", None):
        from marswalk.synthetic import apply_halts
        market = apply_halts(market, reg.halts, gap_open_pct=getattr(reg, "gap_open_pct", 0.0) or 0.0)
    if getattr(reg, "shocks", None):
        from marswalk.synthetic import apply_shocks
        market = apply_shocks(market, reg.shocks)
    pm = getattr(reg, "price_multiplier", None)
    if pm and pm != 1.0:
        market = {sym: [(d, c * pm, iv) for (d, c, iv) in bars]
                  for sym, bars in market.items()}
    p = Params(**overrides)
    res = run_regime(
        reg.id, reg.name, reg.category, reg.rank, universe, market, p,
        earnings=earnings,
        cash_yield_annual=getattr(reg, "cash_yield_annual", None),
    )
    if not res:
        return {"final_return_pct": None, "error": "engine_returned_none"}
    return {
        "final_return_pct": res["final_return_pct"],
        "n_trades": res["n_trades"],
        "n_halt_days": res["n_halt_days"],
        "n_assignments": res["n_assignments"],
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "days": len(res["points"]),
    }


def main():
    universe, regimes = load_config()
    earnings = load_earnings(universe)

    # Sort regimes by rank for consistent output
    regimes.sort(key=lambda r: r.rank)

    out_path = Path("data") / f"longgrind_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    # Grid header
    print()
    print(f"Long-grind countermeasure sweep — {len(regimes)} regimes × {len(CONFIGS)} configs")
    print("=" * 120)
    cfg_names = list(CONFIGS.keys())
    header = "regime".ljust(20) + "  ".join(c.rjust(11) for c in cfg_names)
    print("  " + header)
    print("  " + "-" * (len(header) + 2))

    results: dict[str, dict[str, dict]] = {}
    for reg in regimes:
        results[reg.id] = {}
        cols = []
        for cfg_name, overrides in CONFIGS.items():
            r = run_one(reg, universe, earnings, overrides)
            results[reg.id][cfg_name] = r
            with out_path.open("a") as fh:
                fh.write(json.dumps({"regime": reg.id, "config": cfg_name, **r}) + "\n")
            if r["final_return_pct"] is None:
                cols.append("ERR".rjust(11))
            elif cfg_name == "baseline":
                cols.append(format(r["final_return_pct"], "+.2f").rjust(11))
            else:
                base = results[reg.id]["baseline"]["final_return_pct"]
                if base is None:
                    cols.append("ERR".rjust(11))
                else:
                    delta = r["final_return_pct"] - base
                    cols.append(format(delta, "+.2f").rjust(11))
        print("  " + reg.id.ljust(20) + "  ".join(cols))

    # Summary per candidate
    print()
    print("=" * 120)
    print("Summary: deltas to baseline (in pp), per candidate")
    print("=" * 120)

    for cfg_name in [c for c in cfg_names if c != "baseline"]:
        wins_long_grind = []
        hurts_bulls = []
        wins_3pp = []
        hurts_2pp = []
        for rid, by_cfg in results.items():
            base = by_cfg["baseline"].get("final_return_pct")
            cand = by_cfg[cfg_name].get("final_return_pct")
            if base is None or cand is None:
                continue
            delta = cand - base
            if rid in LONG_GRIND_TARGETS:
                wins_long_grind.append((rid, delta))
            if rid in BULL_TARGETS:
                hurts_bulls.append((rid, delta))
            if delta >= 3:
                wins_3pp.append((rid, delta))
            if delta <= -2:
                hurts_2pp.append((rid, delta))
        print()
        print(f"  CANDIDATE {cfg_name}:")
        print(f"    Long-grind targets ({', '.join(LONG_GRIND_TARGETS)}):")
        for rid, d in wins_long_grind:
            tag = "WIN" if d >= 3 else ("ok" if d > 0 else "FLAT/HURT")
            print(f"      {rid.ljust(20)}  {format(d, '+.2f').rjust(7)}pp  {tag}")
        print("    Bull/grind regimes (don't break):")
        for rid, d in hurts_bulls:
            tag = "HURT >2pp" if d <= -2 else "ok"
            print(f"      {rid.ljust(20)}  {format(d, '+.2f').rjust(7)}pp  {tag}")
        print(f"    Total regimes lifted ≥+3pp: {len(wins_3pp)}")
        print(f"    Total regimes hurt ≤-2pp:   {len(hurts_2pp)}")

    print()
    print(f"Raw results: {out_path}")


if __name__ == "__main__":
    main()
