"""Phase A sweep: bull_2021 lift candidates vs baseline across all 11 regimes.

Four variants (toggled via Params dataclass flags):
  V0    — baseline (detector OFF; current production)
  V1    — Cand-1: in deep-low-IV broad bulls, drop bull_regime_iv_rank_min 50 → 0
  V2    — Cand-2: in deep-low-IV broad bulls, extend DTE 0-7 → 5-14
  V12   — both stacked

Decision rule (from plan):
  - V* lifts bull_2021 by ≥5 pp AND no other regime regresses >2 pp → ship that variant
  - Specifically: grind_2024h1 ≥ +37, ai_2023 ≥ +14, iran_war_2026 ≥ +20.8
  - Nothing clears → ship nothing; the deep-low-IV broad bull is the structural floor

Run:  source .venv/bin/activate && PYTHONPATH=. python scripts/bull2021_sweep.py
"""
from __future__ import annotations

import sys
from dataclasses import replace

from src.marswalk.regimes import load_config
from src.marswalk.data import load_market, load_earnings
from src.marswalk.engine import Params, run_regime


START_CAPITAL = 4_000_000.0


VARIANTS = {
    "V0":  dict(deep_low_iv_detector_enabled=False),
    "V1":  dict(deep_low_iv_detector_enabled=True,  deep_low_iv_drop_iv_floor=True,  deep_low_iv_extend_dte=False),
    "V2":  dict(deep_low_iv_detector_enabled=True,  deep_low_iv_drop_iv_floor=False, deep_low_iv_extend_dte=True),
    "V12": dict(deep_low_iv_detector_enabled=True,  deep_low_iv_drop_iv_floor=True,  deep_low_iv_extend_dte=True),
}


def main():
    universe, regimes = load_config()
    earnings = load_earnings(universe)

    base = Params(start_capital=START_CAPITAL)

    header = f"{'regime':<18}" + "".join(f"{v:>10}" for v in VARIANTS)
    print(header)
    print("-" * len(header))

    results: dict[str, dict[str, dict]] = {v: {} for v in VARIANTS}

    for reg in regimes:
        market = load_market(reg, universe)
        if not market:
            print(f"{reg.id:<18} (no cache)")
            continue

        row_cells = [f"{reg.id:<18}"]
        for label, overrides in VARIANTS.items():
            p = replace(base, **overrides)
            r = run_regime(reg.id, reg.name, reg.category, reg.rank,
                           universe, market, p, earnings=earnings)
            results[label][reg.id] = r
            if r is None:
                row_cells.append(f"{'—':>10}")
                continue
            row_cells.append(f"{r['final_return_pct']:>+8.1f}%")
        print("".join(row_cells))

    # Deltas vs V0
    print()
    print("DELTAS vs V0 (pp; +ve = better) and DD changes:")
    header2 = f"{'regime':<18}" + "".join(f"{v:>16}" for v in VARIANTS if v != "V0")
    print(header2)
    print("-" * len(header2))
    for reg in regimes:
        base_r = results["V0"].get(reg.id)
        if base_r is None:
            continue
        cells = [f"{reg.id:<18}"]
        for label in VARIANTS:
            if label == "V0":
                continue
            r = results[label].get(reg.id)
            if r is None:
                cells.append(f"{'—':>16}")
                continue
            d_ret = r["final_return_pct"] - base_r["final_return_pct"]
            d_dd = r["max_drawdown_pct"] - base_r["max_drawdown_pct"]
            cells.append(f" {d_ret:>+6.1f}/DD{d_dd:>+4.1f} ")
        print("".join(cells))

    # Decision-rule check
    print()
    print("Decision rule check (bull_2021 lift ≥+5 pp; floors: grind≥+37, ai≥+14, iran≥+20.8):")
    for label in VARIANTS:
        if label == "V0":
            continue
        bull_v0 = results["V0"].get("bull_2021")
        bull_r  = results[label].get("bull_2021")
        if not bull_v0 or not bull_r:
            continue
        lift = bull_r["final_return_pct"] - bull_v0["final_return_pct"]
        grind = results[label].get("grind_2024h1", {}).get("final_return_pct", -999)
        ai    = results[label].get("ai_2023", {}).get("final_return_pct", -999)
        iran  = results[label].get("iran_war_2026", {}).get("final_return_pct", -999)
        ok_lift  = lift >= 5.0
        ok_grind = grind >= 37.0
        ok_ai    = ai >= 14.0
        ok_iran  = iran >= 20.8
        ok_all = ok_lift and ok_grind and ok_ai and ok_iran
        flag = "PASS" if ok_all else "FAIL"
        print(f"  {label}: bull_lift={lift:+.1f}pp [{'ok' if ok_lift else 'BAD'}]"
              f"  grind={grind:+.1f}% [{'ok' if ok_grind else 'BAD'}]"
              f"  ai={ai:+.1f}% [{'ok' if ok_ai else 'BAD'}]"
              f"  iran={iran:+.1f}% [{'ok' if ok_iran else 'BAD'}]  → {flag}")


if __name__ == "__main__":
    sys.exit(main() or 0)
