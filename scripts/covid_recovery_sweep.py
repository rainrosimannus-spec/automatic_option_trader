"""Phase 2 sweep: covid recovery adaptations vs baseline.

Four variants (toggled via Params dataclass flags):
  V0    — baseline (Levers A/B/D all OFF)
  VA    — Lever A only (direction-aware VIX gate, parity fix with live)
  VAB   — A + B (recovery override releases dd_mult for 3 days)
  VABD  — A + B + D (CC delta tightens during recovery window)

Decision rule (from plan):
  - VABD lifts covid_2020 by ≥10 pp AND no other regime regresses by >2 pp → commit ABD
  - VAB does it alone → commit AB, drop D
  - Only VA helps → commit A as parity fix; accept covid floor
  - Nothing helps → commit A as parity fix anyway; honest report

Run:  source .venv/bin/activate && PYTHONPATH=. python scripts/covid_recovery_sweep.py
"""
from __future__ import annotations

import sys
from dataclasses import replace

from src.marswalk.regimes import load_config
from src.marswalk.data import load_market, load_earnings
from src.marswalk.engine import Params, run_regime


START_CAPITAL = 4_000_000.0


VARIANTS = {
    "V0":   dict(direction_aware_vix_gate=False, recovery_override_enabled=False, fast_recovery_cc_enabled=False),
    "VA":   dict(direction_aware_vix_gate=True,  recovery_override_enabled=False, fast_recovery_cc_enabled=False),
    "VAB":  dict(direction_aware_vix_gate=True,  recovery_override_enabled=True,  fast_recovery_cc_enabled=False),
    "VABD": dict(direction_aware_vix_gate=True,  recovery_override_enabled=True,  fast_recovery_cc_enabled=True),
}


def main():
    universe, regimes = load_config()
    earnings = load_earnings(universe)

    base = Params(start_capital=START_CAPITAL)

    # Header
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
    header2 = f"{'regime':<18}" + "".join(f"{v:>14}" for v in VARIANTS if v != "V0")
    print(header2)
    print("-" * len(header2))
    for reg in regimes:
        if reg.id not in results["V0"]:
            continue
        base_r = results["V0"].get(reg.id)
        if base_r is None:
            continue
        cells = [f"{reg.id:<18}"]
        for label in VARIANTS:
            if label == "V0":
                continue
            r = results[label].get(reg.id)
            if r is None:
                cells.append(f"{'—':>14}")
                continue
            d_ret = r["final_return_pct"] - base_r["final_return_pct"]
            d_dd = r["max_drawdown_pct"] - base_r["max_drawdown_pct"]
            cells.append(f"{d_ret:>+6.1f}/DD{d_dd:>+4.1f} ")
        print("".join(cells))

    # Trade counts for covid + bear_2022 (most relevant)
    print()
    print("Trade counts per variant (covid + bear_2022 — diagnostic):")
    for rid in ("covid_2020", "bear_2022", "carry_2024", "iran_war_2026"):
        line = f"  {rid:<18}"
        for label in VARIANTS:
            r = results[label].get(rid)
            if r is None:
                line += f" {label}:   —    "
                continue
            line += f" {label}:{r['n_trades']:>4}t/{r['n_assignments']:>3}a/{r['n_halt_days']:>3}h "
        print(line)


if __name__ == "__main__":
    sys.exit(main() or 0)
