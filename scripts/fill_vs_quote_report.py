#!/usr/bin/env python
"""Fill-vs-quote report: how option fills landed against the decision-time quote,
split into BEFORE and AFTER a cutoff date (default: the OPRA live-feed subscription,
2026-10-13; the live feed only applies from the first gateway login after it,
so pass that restart time, e.g. "2026-10-14 06:00"). Read-only on data/options.db.

    .venv/bin/python scripts/fill_vs_quote_report.py            # default cutoff
    .venv/bin/python scripts/fill_vs_quote_report.py 2026-10-14 # custom cutoff
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.broker.trade_sync import fill_vs_quote  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "options.db"
CUTOFF = sys.argv[1] if len(sys.argv) > 1 else "2026-10-14"
SINCE = sys.argv[2] if len(sys.argv) > 2 else "2026-07-01"


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select trade_type, fill_price, bid_at_entry, ask_at_entry, mid_at_entry, created_at "
        "from trades where order_status='FILLED' and fill_price>0 and created_at>=? "
        "and trade_type in ('SELL_PUT','SELL_CALL','BUY_PUT','BUY_CALL') order by created_at",
        (SINCE,)).fetchall()
    buckets: dict = {}
    for ttype, fill, bid, ask, mid, at in rows:
        period = "after" if at >= CUTOFF else "before"
        m = fill_vs_quote(ttype, fill, bid, ask, mid)
        b = buckets.setdefault((ttype, period), {"n": 0, "quoted": 0, "spread": [], "fmm": [], "cap": [], "fill": []})
        b["n"] += 1
        b["fill"].append(fill)
        if m:
            b["quoted"] += 1
            b["spread"].append(m["spread"]); b["fmm"].append(m["fill_minus_mid"]); b["cap"].append(m["capture_pct"])
    print(f"Option fills since {SINCE}, cutoff {CUTOFF} (before = delayed quotes, after = live OPRA)")
    print(f"{'type':10} {'period':7} {'fills':>5} {'quoted':>6} {'avg fill':>9} {'avg spread':>10} {'fill-mid':>9} {'capture%':>9}")
    for (ttype, period) in sorted(buckets):
        b = buckets[(ttype, period)]
        print(f"{ttype:10} {period:7} {b['n']:5} {b['quoted']:6} {_avg(b['fill']) or '-':>9} "
              f"{_avg(b['spread']) if b['spread'] else '-':>10} {_avg(b['fmm']) if b['fmm'] else '-':>9} "
              f"{_avg(b['cap']) if b['cap'] else '-':>9}")
    print("\ncapture%: 0 = filled at the near touch (bid for sells, ask for buys), 50 = at mid, 100 = far touch.")
    print("Decide after ~a month of 'after' rows: if sells capture no more of a (narrower) spread, cancel OPRA.")


if __name__ == "__main__":
    main()
