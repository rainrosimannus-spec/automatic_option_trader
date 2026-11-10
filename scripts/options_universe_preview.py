"""Preview the options-universe filter (Rain 2026-11-11) without changing anything live.

Reads config/options_universe.yaml (the screener's output — untouched), fetches ~1y of FMP
closes per name (cached per day in data/options_universe_filter.json), and prints the ≤50 kept
names with their risk metrics and every dropped name with the rule(s) it failed.

Usage:
    .venv/bin/python scripts/options_universe_preview.py
    .venv/bin/python scripts/options_universe_preview.py --max-vol 0.45 --max-names 40
Turn it on for the trader with `options_universe_filter: {enabled: true}` in settings.yaml + restart.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.core.config import get_settings  # noqa: E402
from src.strategy.universe_filter import (load_raw_options_universe, build_metrics,  # noqa: E402
                                          select_options_universe)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-vol", type=float); ap.add_argument("--max-dd", type=float)
    ap.add_argument("--worst-day", type=float); ap.add_argument("--max-breach", type=float)
    ap.add_argument("--max-price", type=float); ap.add_argument("--max-names", type=int)
    ap.add_argument("--keep-breakthrough", action="store_true")
    a = ap.parse_args()
    cfg = get_settings().options_universe_filter.model_copy()
    if a.max_vol is not None: cfg.max_ann_vol = a.max_vol
    if a.max_dd is not None: cfg.max_drawdown_1y = a.max_dd
    if a.worst_day is not None: cfg.worst_day_floor = a.worst_day
    if a.max_breach is not None: cfg.max_breach_rate = a.max_breach
    if a.max_price is not None: cfg.max_share_price = a.max_price
    if a.max_names is not None: cfg.max_names = a.max_names
    if a.keep_breakthrough: cfg.exclude_tiers = []
    entries = load_raw_options_universe()
    print(f"screener file: {len(entries)} names; rules: tiers out {cfg.exclude_tiers}, vol ≤ {cfg.max_ann_vol:.0%}, "
          f"dd1y ≤ {cfg.max_drawdown_1y:.0%}, worst day ≥ {cfg.worst_day_floor:.0%}, breach ≤ {cfg.max_breach_rate:.0%}, "
          f"liquidity ≥ {cfg.min_liquidity:.0f}, price ≤ {cfg.max_share_price:.0f}, max {cfg.max_names}")
    metrics = build_metrics([str(e["symbol"]) for e in entries], cfg)
    kept, dropped = select_options_universe(entries, metrics, cfg)

    def row(e):
        m = metrics.get(str(e["symbol"])) or {}
        f = lambda k, fmt: (fmt.format(m[k]) if m.get(k) is not None else "   n/a")
        return (f"{e['symbol']:6} {str(e.get('tier',''))[:6]:6} {float(e.get('options_score') or 0):5.1f} "
                f"{f('ann_vol','{:6.0%}')} {f('max_dd_1y','{:6.0%}')} {f('worst_day','{:7.1%}')} "
                f"{f('breach_rate','{:6.0%}')} {f('last_close','{:7.0f}')}  {e.get('currency','')}")
    hdr = f"{'sym':6} {'tier':6} {'score':>5} {'vol':>6} {'dd1y':>6} {'worst':>7} {'breach':>6} {'price':>7}  ccy"
    print(f"\nKEPT ({len(kept)}):\n  {hdr}")
    for e in kept: print("  " + row(e))
    print(f"\nDROPPED ({len(dropped)}):\n  {hdr}  reasons")
    for e, why in sorted(dropped, key=lambda x: -float(x[0].get('options_score') or 0)):
        print("  " + row(e) + "  " + ", ".join(why))


if __name__ == "__main__":
    main()
