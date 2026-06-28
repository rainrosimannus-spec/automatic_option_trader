#!/usr/bin/env python
"""Execution-quality report — how much premium are we leaving on the table by selling at the bid?

Reads the decision-time quote (bid/ask/mid) now captured on option-sell Trades (see trade_sync /
put_seller / wheel) and reports fill-vs-mid leakage + the annualised value of capturing it with a
mid→bid price walk. Run after a few weeks of live fills:

    python scripts/exec_quality.py

Until trades accumulate with mid_at_entry populated it will report 0 instrumented trades — that's
expected; it confirms the plumbing and gives you the tool to decide on the price-walk with REAL data.
"""
import sqlite3
import glob
import statistics as st
from datetime import date


def report(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    tabs = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "trades" not in tabs:
        con.close()
        return
    cols = [r[1] for r in cur.execute("PRAGMA table_info(trades)")]
    if "mid_at_entry" not in cols:
        con.close()
        return

    total_sells = cur.execute(
        "SELECT COUNT(*) FROM trades WHERE trade_type IN ('SELL_PUT','SELL_CALL')"
    ).fetchone()[0]
    rows = list(cur.execute(
        """SELECT premium, fill_price, quantity, bid_at_entry, ask_at_entry, mid_at_entry, created_at
           FROM trades
           WHERE trade_type IN ('SELL_PUT','SELL_CALL')
             AND mid_at_entry IS NOT NULL AND mid_at_entry > 0
             AND fill_price IS NOT NULL AND fill_price > 0"""
    ))
    con.close()

    print(f"\n=== {db_path} ===")
    print(f"  SELL option trades: {total_sells} | instrumented (mid_at_entry present): {len(rows)}")
    if not rows:
        print("  → no instrumented fills yet; run again after a few weeks of trading.")
        return

    leak_dollars = 0.0       # premium left vs mid (only when fill < mid)
    captured_dollars = 0.0   # already better than bid (IBKR/our pricing improvement over the bid)
    spread_pct = []
    leak_pct = []
    dates = []
    for prem, fill, qty, bid, ask, mid, ts in rows:
        q = qty or 1
        if not bid or not ask or ask <= bid:
            continue
        spread = ask - bid
        spread_pct.append(spread / mid * 100 if mid else 0)
        leak_ps = max(0.0, mid - fill)          # how far below mid we sold (per share)
        leak_dollars += leak_ps * 100 * q
        captured_dollars += max(0.0, fill - bid) * 100 * q
        leak_pct.append((mid - fill) / fill * 100 if fill else 0)
        if ts:
            dates.append(str(ts)[:10])

    dates = sorted(d for d in dates if d)
    span = max(1, (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days) if dates else 1
    print(f"  avg bid-ask spread: {st.mean(spread_pct):.1f}% of mid")
    print(f"  avg fill vs mid:    {st.mean(leak_pct):+.1f}% of premium (negative = sold below mid)")
    print(f"  premium left vs mid (gross): ${leak_dollars:,.0f} over {span}d")
    print(f"  already-captured over bid:   ${captured_dollars:,.0f}")
    # A realistic mid→bid walk captures ~half of the bid→mid gap (you give some back to fills).
    print(f"  est. recoverable @50% walk:  ${leak_dollars * 0.5:,.0f}  "
          f"(annualised ≈ ${leak_dollars * 0.5 / span * 365:,.0f})")


def main():
    for db in sorted(glob.glob("data/*.db")):
        report(db)


if __name__ == "__main__":
    main()
