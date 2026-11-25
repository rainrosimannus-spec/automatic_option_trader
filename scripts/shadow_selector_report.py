#!/usr/bin/env python3
"""Shadow selector vs the live screener, day by day (Rain 2026-11-25). Read-only.

  .venv/bin/python scripts/shadow_selector_report.py [START] [END]

Shadow rows come from data/shadow_selector.jsonl (what the SIMULATOR's price-only rule would have
sold, paper-booked with real bids, settled at the FMP close). Live rows are the account's real
SELL_PUT fills in data/options.db over the same dates, settled the same way, so both sides are
judged per contract on premium − intrinsic at expiry. The question it answers after 2-3 months:
does the sim's selector pick better contracts than the screener on the same days?
"""
import sys, sqlite3, collections
from datetime import date, datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.core.config import get_settings
from src.strategy.shadow_selector import read_jsonl, unsettled, fetch_dated, close_on_date

cfg = get_settings().shadow_selector
rows = read_jsonl(ROOT / cfg.log_path)
sells = [r for r in rows if r.get("kind") == "sell"]
if not sells:
    print("no shadow sells yet (data/shadow_selector.jsonl)"); sys.exit(0)
START = sys.argv[1] if len(sys.argv) > 1 else min(r["date"] for r in sells)
END = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
settles = {(r["sym"], float(r["strike"]), r["expiry"], r["sold"]): r for r in rows if r.get("kind") == "settle"}

def shadow_rows():
    for r in sells:
        if not (START <= r["date"] <= END):
            continue
        s = settles.get((r["sym"], float(r["strike"]), r["expiry"], r["date"]))
        yield {"date": r["date"], "sym": r["sym"], "strike": r["strike"], "expiry": r["expiry"], "qty": r.get("qty", 1),
               "premium": r["premium"], "otm": r.get("otm_pct"), "intrinsic": (s["intrinsic"] if s else None)}

_cache = {}
def live_rows():
    c = sqlite3.connect(f"file:{ROOT}/data/options.db?mode=ro", uri=True)
    q = c.execute("""select symbol, strike, expiry, substr(created_at,1,10), quantity, fill_price
                     from trades where trade_type='SELL_PUT' and order_status='FILLED' and date(created_at)>=? and date(created_at)<=?
                     and strike>0 and expiry!=''""", (START, END)).fetchall()
    for sym, K, exp, d, qty, px in q:
        e = datetime.strptime(exp, "%Y%m%d").date()
        intr = None
        if e < date.today():
            key = (sym, exp)
            if key not in _cache:
                _cache[key] = close_on_date(fetch_dated(sym, e - timedelta(days=6), e + timedelta(days=1)), exp)
            if _cache[key] is not None:
                intr = max(0.0, K - _cache[key])
        yield {"date": d, "sym": sym, "strike": K, "expiry": exp, "qty": int(qty), "premium": float(px), "otm": None, "intrinsic": intr}

def summarise(label, rs):
    rs = list(rs); done = [r for r in rs if r["intrinsic"] is not None]
    ctr = sum(r["qty"] for r in rs); dctr = sum(r["qty"] for r in done) or 1
    prem = sum(r["premium"] * 100 * r["qty"] for r in done); intr = sum(r["intrinsic"] * 100 * r["qty"] for r in done)
    itm = sum(r["qty"] for r in done if r["intrinsic"] > 0)
    names = collections.Counter(r["sym"] for r in rs).most_common(6)
    print(f"{label:<8} orders {len(rs):3d}  contracts {ctr:3d}  settled {dctr:3d}  ITM {itm/dctr:4.0%}  "
          f"premium/ctr ${prem/dctr:6.0f}  intrinsic/ctr ${intr/dctr:6.0f}  net/ctr ${(prem-intr)/dctr:+6.0f}  net ${prem-intr:+,.0f}")
    print(f"         most sold: {', '.join(f'{s}×{n}' for s, n in names)}")
    return rs

print(f"shadow selector vs live screener, {START} → {END} (per-contract, premium − intrinsic at expiry, before commissions)\n")
sh = summarise("shadow", shadow_rows()); lv = summarise("live", live_rows())
both = {r["sym"] for r in sh} & {r["sym"] for r in lv}
print(f"\nnames both sold: {sorted(both) or 'none'}")
wk = collections.defaultdict(lambda: {"shadow": [0, 0.0], "live": [0, 0.0]})
for lab, rs in (("shadow", sh), ("live", lv)):
    for r in rs:
        if r["intrinsic"] is None: continue
        w = datetime.fromisoformat(r["date"]).strftime("%G-W%V"); wk[w][lab][0] += r["qty"]; wk[w][lab][1] += (r["premium"] - r["intrinsic"]) * 100 * r["qty"]
print("\nweek       shadow ctr  net/ctr    live ctr  net/ctr")
for w in sorted(wk):
    s, l = wk[w]["shadow"], wk[w]["live"]
    print(f"{w}  {s[0]:9d}  {s[1]/s[0] if s[0] else 0:+7.0f}  {l[0]:10d}  {l[1]/l[0] if l[0] else 0:+7.0f}")
