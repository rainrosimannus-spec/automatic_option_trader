"""Run dotcom_2000 + oil_crash_2014 and persist to marswalk.db so they
show up on the /marswalk dashboard alongside the others.
"""
from __future__ import annotations

import sys

from src.marswalk.regimes import load_config
from src.marswalk.data import load_market, load_earnings
from src.marswalk.engine import Params, run_regime, save_run
from src.marswalk.service import _replace_prior


TARGETS = ("dotcom_2000", "oil_crash_2014")
START_CAPITAL = 4_000_000.0


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)
    params = Params(start_capital=START_CAPITAL)

    for rid in TARGETS:
        reg = by_id.get(rid)
        if reg is None:
            print(f"!! {rid} not in marswalk_regimes.yaml")
            continue
        market = load_market(reg, universe)
        if not market:
            print(f"!! {rid}: no cached data — run scripts/fetch_new_regimes.py first")
            continue
        names_covered = sum(1 for s in universe if s in market)
        print(f"\n=== {rid} ({names_covered}/{len(universe)} names) ===")
        res = run_regime(reg.id, reg.name, reg.category, reg.rank,
                         universe, market, params, earnings=earnings)
        if not res:
            print(f"  engine returned no result")
            continue
        _replace_prior(reg.id, params)
        save_run(res)
        print(f"  final_return = {res['final_return_pct']:+.1f}%  "
              f"max_dd = {res['max_drawdown_pct']:.1f}%  "
              f"trades = {res['n_trades']}  assignments = {res['n_assignments']}  "
              f"halt_days = {res['n_halt_days']}")


if __name__ == "__main__":
    sys.exit(main() or 0)
