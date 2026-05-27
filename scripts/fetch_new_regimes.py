"""Fetch market data for dotcom_2000 + oil_crash_2014 via Yahoo.

Uses fetch_symbols_yahoo (per-name daily close + 20d realized-vol IV proxy)
plus fetch_spy_yahoo and fetch_vix_yahoo for the engine's MA200 / VIX gates.
No IBKR contention — pure HTTPS to Yahoo v8 chart endpoint.

Run:  source .venv/bin/activate && PYTHONPATH=. python scripts/fetch_new_regimes.py
"""
from __future__ import annotations

import sys

from src.marswalk.regimes import load_config
from src.marswalk.data import (
    fetch_symbols_yahoo,
    fetch_spy_yahoo,
    fetch_vix_yahoo,
    has_data,
)


TARGETS = ("dotcom_2000", "oil_crash_2014")


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}

    for rid in TARGETS:
        reg = by_id.get(rid)
        if reg is None:
            print(f"!! {rid} not in marswalk_regimes.yaml")
            continue
        print(f"\n=== {rid} ({reg.start} → {reg.end}) ===")

        print(f"  Fetching ^SPY ...")
        fetch_spy_yahoo(reg)
        print(f"  Fetching ^VIX ...")
        fetch_vix_yahoo(reg)

        print(f"  Fetching {len(universe)} universe symbols ...")
        fetch_symbols_yahoo(reg, list(universe))

        # Coverage report
        covered = sum(1 for s in universe if has_data(rid, s))
        print(f"  Coverage: {covered}/{len(universe)} symbols cached")


if __name__ == "__main__":
    sys.exit(main() or 0)
