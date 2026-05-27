"""Phase A diagnostic: confirm bull_2021 has structurally lower universe-median
IV-rank than the other bull-like regimes (grind_2024h1, ai_2023).

Hypothesis: bull_2021 medians sit ~15-25% IV-rank throughout the regime, vs
grind/ai medians at 30-50%. If confirmed, the existing bull_regime_iv_rank_min=50
gate is a NARROW-LEADERSHIP detector that skips most candidates in deep-low-IV
broad bulls — and a `deep_low_iv` detector (universe-median IV-rank < 30 over
N trailing trading days) can selectively branch behavior only in those regimes.

Mirrors the engine's iv_rank_lut construction exactly so the numbers are
apples-to-apples with what the trading loop sees.

Run:  source .venv/bin/activate && PYTHONPATH=. python scripts/bull2021_iv_diagnostic.py
"""
from __future__ import annotations

import statistics
import sys

from src.marswalk.regimes import load_config
from src.marswalk.data import load_market


TARGETS = ("bull_2021", "grind_2024h1", "ai_2023", "iran_war_2026", "chop_2023h2")


def main():
    universe, regimes = load_config()
    reg_by_id = {r.id: r for r in regimes}

    print(f"{'regime':<18} {'p50':>8} {'p25':>8} {'p75':>8} {'min_p50':>10} {'max_p50':>10} {'days':>6}")
    print("-" * 70)

    for rid in TARGETS:
        reg = reg_by_id.get(rid)
        if reg is None:
            print(f"{rid:<18} (not in config)")
            continue
        market = load_market(reg, universe)
        if not market:
            print(f"{rid:<18} (no cached market data)")
            continue

        # Replicate engine's iv_rank_lut construction.
        iv_rank_lut: dict[str, dict] = {}
        for sym, bars in market.items():
            if sym == "^VIX" or sym.startswith("_pre:") or sym == "^SPY":
                continue
            rmin = rmax = None
            per_date = {}
            for (bd, _c, biv) in bars:
                if biv and biv > 0:
                    rmin = biv if rmin is None else min(rmin, biv)
                    rmax = biv if rmax is None else max(rmax, biv)
                    per_date[bd] = (rmin, rmax, biv)
                else:
                    per_date[bd] = None
            iv_rank_lut[sym] = per_date

        def ivr_at(sym, d):
            rec = iv_rank_lut.get(sym, {}).get(d)
            if not rec:
                return None
            rmin, rmax, iv = rec
            if rmax - rmin < 1e-9:
                return 50.0
            return (iv - rmin) / (rmax - rmin) * 100.0

        # Collect all unique dates across all symbols.
        all_dates: set = set()
        for sym, bars in market.items():
            if sym.startswith("_pre:") or sym in ("^VIX", "^SPY"):
                continue
            for (bd, _c, _iv) in bars:
                all_dates.add(bd)
        dates = sorted(all_dates)

        # Per-day universe median.
        daily_medians: list[float] = []
        for d in dates:
            ivrs = []
            for sym in iv_rank_lut:
                v = ivr_at(sym, d)
                if v is not None:
                    ivrs.append(v)
            if len(ivrs) >= 5:  # require minimum coverage
                daily_medians.append(statistics.median(ivrs))

        if not daily_medians:
            print(f"{rid:<18} (no daily medians computed)")
            continue

        p50 = statistics.median(daily_medians)
        sorted_d = sorted(daily_medians)
        p25 = sorted_d[len(sorted_d) // 4]
        p75 = sorted_d[(3 * len(sorted_d)) // 4]
        print(f"{rid:<18} {p50:>7.1f}% {p25:>7.1f}% {p75:>7.1f}% {min(daily_medians):>9.1f}% {max(daily_medians):>9.1f}% {len(daily_medians):>6}")

    print()
    print("Reading the table:")
    print("  p50 = median of daily universe-median IV-ranks across the regime.")
    print("  If bull_2021 p50 < 30 AND grind/ai p50 > 30 → 'deep_low_iv'")
    print("  detector with threshold ~30 will fire selectively in bull_2021.")


if __name__ == "__main__":
    sys.exit(main() or 0)
