#!/usr/bin/env python
"""Confirm the entry-timing edge on the REAL compounder buys so far (not the synthetic universe).

For every actual portfolio buy (portfolio_transactions.action='buy'), pull that trading day's true OHLC
and ask: would executing in the LATE session (≈ the CLOSE) have beaten (a) the day's OPEN and (b) what we
ACTUALLY paid? This is the real-data check behind the late-session-window change.

Excludes XEON (cash-park ETF, not a growth buy) and sub-$500 dust. USD names only for the clean read
(foreign RTH/venue timing differs); foreign shown separately as best-effort where FMP has the listing.
"""
import sys, os, json, sqlite3, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from src.portfolio.fmp import get_fmp_key

# FMP suffix per (currency) for the foreign best-effort leg
FMP_SUFFIX = {"GBP": ".L", "CAD": ".TO", "EUR": ".AS"}   # AZN London, SU/CNQ Toronto, INGA Amsterdam


def load_buys():
    con = sqlite3.connect("data/trades.db"); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT symbol, substr(created_at,1,10) d, price, currency, amount, shares
                          FROM portfolio_transactions WHERE action='buy' ORDER BY created_at""").fetchall()
    con.close()
    out = []
    for r in rows:
        if r["symbol"] == "XEON":            # cash park, not a compounder growth buy
            continue
        if (r["amount"] or 0) < 500:         # dust / FX crumbs
            continue
        out.append(dict(symbol=r["symbol"], date=r["d"], fill=float(r["price"]),
                        ccy=r["currency"], amount=float(r["amount"]), shares=float(r["shares"])))
    return out


def fmp_ticker(sym, ccy):
    return sym + FMP_SUFFIX.get(ccy, "") if ccy != "USD" else sym


def fetch_ohlc_range(ticker, start, end, key):
    url = (f"https://financialmodelingprep.com/stable/historical-price-eod/full"
           f"?symbol={ticker}&from={start}&to={end}&apikey={key}")
    try:
        r = requests.get(url, timeout=25); r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("historical", []) if data else [])
        return {row["date"]: (float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
                for row in rows if row.get("open") and row.get("close")}
    except Exception as e:
        print(f"  FAIL {ticker}: {e}")
        return {}


def main():
    buys = load_buys()
    key = get_fmp_key()
    # fetch each distinct ticker once over the whole buy span
    span_lo = min(b["date"] for b in buys); span_hi = max(b["date"] for b in buys)
    tickers = {}
    for b in buys:
        tk = fmp_ticker(b["symbol"], b["ccy"])
        tickers.setdefault(tk, (b["symbol"], b["ccy"]))
    ohlc = {}
    print(f"fetching OHLC for {len(tickers)} tickers, {span_lo}..{span_hi}")
    for tk in tickers:
        ohlc[tk] = fetch_ohlc_range(tk, span_lo, span_hi, key)
        time.sleep(0.15)

    rows = []
    for b in buys:
        tk = fmp_ticker(b["symbol"], b["ccy"])
        bar = ohlc.get(tk, {}).get(b["date"])
        if not bar:
            rows.append({**b, "open": None, "close": None, "have": False})
            continue
        o, h, l, c = bar
        rows.append({**b, "open": o, "close": c,
                     "close_vs_open": c / o - 1.0,
                     "close_vs_fill": c / b["fill"] - 1.0,
                     "open_vs_fill": o / b["fill"] - 1.0,
                     "have": True})

    def report(label, subset):
        s = [r for r in subset if r.get("have")]
        if not s:
            print(f"\n### {label}: no OHLC coverage"); return
        amt = np.array([r["amount"] for r in s])
        cvo = np.array([r["close_vs_open"] for r in s])
        cvf = np.array([r["close_vs_fill"] for r in s])
        ovf = np.array([r["open_vs_fill"] for r in s])
        W = amt.sum()
        print(f"\n### {label}  (n={len(s)} buys, ${W:,.0f} notional)")
        print(f"  close vs open :  mean {100*cvo.mean():+.3f}%   $-wtd {100*(cvo*amt).sum()/W:+.3f}%   "
              f"close<open on {100*np.mean(cvo<0):.0f}% of buys")
        print(f"  close vs FILL :  mean {100*cvf.mean():+.3f}%   $-wtd {100*(cvf*amt).sum()/W:+.3f}%   "
              f"close<fill on {100*np.mean(cvf<0):.0f}% of buys")
        print(f"  open  vs FILL :  mean {100*ovf.mean():+.3f}%   $-wtd {100*(ovf*amt).sum()/W:+.3f}%")
        saved = -(cvf*amt).sum()
        print(f"  => late-session (close) vs actual fills would have {'SAVED' if saved>0 else 'COST'} "
              f"${abs(saved):,.0f} on ${W:,.0f} deployed ({100*saved/W:+.3f}%)")

    usd = [r for r in rows if r["ccy"] == "USD"]
    fx  = [r for r in rows if r["ccy"] != "USD"]
    report("USD names (clean read)", usd)
    report("Foreign names (best-effort)", fx)
    report("ALL names", rows)

    print("\nper-buy detail (USD):")
    print(f"  {'date':<11}{'sym':<7}{'fill':>10}{'open':>10}{'close':>10}{'c/o%':>8}{'c/fill%':>9}  ${'amt':>9}")
    for r in sorted(usd, key=lambda x: x["date"]):
        if not r.get("have"):
            print(f"  {r['date']:<11}{r['symbol']:<7}{r['fill']:>10.2f}{'—':>10}{'—':>10}{'no data':>17}"); continue
        print(f"  {r['date']:<11}{r['symbol']:<7}{r['fill']:>10.2f}{r['open']:>10.2f}{r['close']:>10.2f}"
              f"{100*r['close_vs_open']:>+8.2f}{100*r['close_vs_fill']:>+9.2f}  {r['amount']:>10,.0f}")

    missing = sorted({fmp_ticker(r['symbol'], r['ccy']) for r in rows if not r.get('have')})
    if missing:
        print(f"\nno OHLC for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
