"""Offline sweep: VIX-tiered margin cap variants vs flat-80% baseline.

Loads each regime's cached market data once, then runs the engine with four
Params variants. Reports raw return, max DD, and (for windows ≥90d) annualized.

Run:  source .venv/bin/activate && python scripts/margin_tier_sweep.py
"""
from __future__ import annotations

import sys
from dataclasses import replace

from src.marswalk.regimes import load_config
from src.marswalk.data import load_market, load_earnings
from src.marswalk.engine import Params, run_regime


VARIANTS = {
    "baseline (flat 80%)": dict(),
    "A 80/60/40":          dict(margin_cap_low_vix=0.80, margin_cap_mid_vix=0.60, margin_cap_high_vix=0.40),
    "B 80/50/30":          dict(margin_cap_low_vix=0.80, margin_cap_mid_vix=0.50, margin_cap_high_vix=0.30),
    "C 70/50/30":          dict(margin_cap_low_vix=0.70, margin_cap_mid_vix=0.50, margin_cap_high_vix=0.30),
}


def annualize(ret_pct: float, n_days: int) -> float | None:
    if n_days < 90:
        return None
    yrs = n_days / 365.0
    g = 1.0 + ret_pct / 100.0
    if g <= 0:
        return -100.0
    return (g ** (1.0 / yrs) - 1.0) * 100.0


def main():
    universe, regimes = load_config()
    earnings = load_earnings(universe)

    base_params = Params()

    print(f"{'regime':<18} {'days':>5}  " + "  ".join(f"{v:^22}" for v in VARIANTS) + "  notes")
    print("-" * (24 + 24 * len(VARIANTS)))

    rows: list[tuple[str, int, dict]] = []
    for reg in regimes:
        market = load_market(reg, universe)
        if not market:
            print(f"{reg.id:<18}   (no cached market data — skipped)")
            continue
        n_days = sum(1 for s in market if not s.startswith("_pre:") and s != "^VIX" and s != "^SPY")
        # Use the actual date range from cached bars
        try:
            sample_bars = next(iter([market[s] for s in market if not s.startswith("_pre:") and s != "^VIX" and s != "^SPY"]))
            n_days = len({b[0] for b in sample_bars})
        except StopIteration:
            n_days = 0

        results = {}
        for label, overrides in VARIANTS.items():
            p = replace(base_params, **overrides)
            r = run_regime(reg.id, reg.name, reg.category, reg.rank,
                           universe, market, p, earnings=earnings)
            if r is None:
                results[label] = None
                continue
            ann = annualize(r["final_return_pct"], n_days)
            results[label] = {
                "ret": r["final_return_pct"],
                "dd": r["max_drawdown_pct"],
                "ann": ann,
                "trades": r["n_trades"],
                "halt": r["n_halt_days"],
            }

        # Format the row
        cells = []
        for label in VARIANTS:
            r = results[label]
            if r is None:
                cells.append(f"{'—':^22}")
                continue
            ann_str = f"{r['ann']:+5.0f}%/y" if r["ann"] is not None else f"{r['ret']:+5.1f}%   "
            dd_str = f"DD{r['dd']:.0f}"
            cells.append(f"{ann_str} {dd_str:>5}".center(22))
        print(f"{reg.id:<18} {n_days:>5}  " + "  ".join(cells))

        rows.append((reg.id, n_days, results))

    # Summary deltas (variant minus baseline) for annualized + DD
    print()
    print("DELTAS vs baseline (positive = variant better):")
    print(f"{'regime':<18} " + "  ".join(f"{v:^22}" for v in VARIANTS if v != "baseline (flat 80%)"))
    print("-" * (20 + 24 * (len(VARIANTS) - 1)))
    for reg_id, n_days, results in rows:
        base = results.get("baseline (flat 80%)")
        if base is None:
            continue
        cells = []
        for label in VARIANTS:
            if label == "baseline (flat 80%)":
                continue
            r = results.get(label)
            if r is None:
                cells.append(f"{'—':^22}")
                continue
            if base["ann"] is not None and r["ann"] is not None:
                d_ret = r["ann"] - base["ann"]
                ret_str = f"{d_ret:+5.1f}pp/y"
            else:
                d_ret = r["ret"] - base["ret"]
                ret_str = f"{d_ret:+5.1f}pp  "
            d_dd = base["dd"] - r["dd"]  # smaller DD is better; show as positive improvement
            dd_str = f"DD{d_dd:+.0f}"
            cells.append(f"{ret_str} {dd_str:>6}".center(22))
        print(f"{reg_id:<18} " + "  ".join(cells))


if __name__ == "__main__":
    sys.exit(main() or 0)
