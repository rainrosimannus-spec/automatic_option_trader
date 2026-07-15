#!/usr/bin/env python
"""Does the late-session edge REVERSE for YELLOW (extended) names?

Green names (below fair value, pulling back) drift DOWN intraday -> buy late = cheaper (confirmed).
Yellow names (above fair value, near 52w-high, momentum) may drift UP intraday -> buy late = OVERPAY.
If so, the late-session change must be GREEN-ONLY and yellow keeps the current early-buy behaviour.

Test: classify the live USD watchlist into green/yellow via fair_price_attractiveness, take the top
N of each by compounder rank, pull the last ~10 trading days of true OHLC, and compare intraday drift
(close vs open). Mirrors 'buy one top name/day, $60k each' — a percentage-drift measurement.
"""
import sys, os, sqlite3, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from src.portfolio.fmp import get_fmp_key
from src.portfolio import compounder as cmp
from src.portfolio.compounder import NameInput
from src.portfolio.config import CompounderConfig

CC = CompounderConfig()
TIER_BUDGETS = {"growth": CC.tier_growth, "breakthrough": CC.tier_breakthrough, "dividend": CC.tier_dividend}
N_EACH = 10
LOOKBACK_DAYS = 20   # calendar; keep the last 10 trading days


def load_watchlist():
    con = sqlite3.connect("data/trades.db"); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT symbol,tier,currency,current_price,sma_200,high_52w,momentum_12_1,
        growth_score,forward_growth_score,quality_score,valuation_score,
        dividend_total_return_score,risk_total_penalty FROM portfolio_watchlist""").fetchall()
    con.close()
    out = []
    for r in rows:
        if (r["currency"] or "USD") != "USD":            # FMP blocks foreign listings on this key
            continue
        if r["symbol"] in ("XEON", "SGOV", "XFFE"):
            continue
        px, sma, hi = r["current_price"], r["sma_200"], r["high_52w"]
        if not px or px <= 0 or not sma:
            continue
        out.append(dict(r))
    return out


def fetch_ohlc(sym, start, key):
    url = (f"https://financialmodelingprep.com/stable/historical-price-eod/full"
           f"?symbol={sym}&from={start}&apikey={key}")
    try:
        r = requests.get(url, timeout=25); r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("historical", []) if data else [])
        return sorted(([row["date"], float(row["open"]), float(row["close"])]
                       for row in rows if row.get("open") and row.get("close")), key=lambda x: x[0])
    except Exception as e:
        print(f"  FAIL {sym}: {e}"); return []


def main():
    wl = load_watchlist()
    # build ranked universe + classify green/yellow by fair-value attractiveness
    names = [NameInput(symbol=r["symbol"], tier=(r["tier"] if r["tier"] in TIER_BUDGETS else "growth"),
                growth=r["growth_score"] or 0, forward_growth=r["forward_growth_score"] or 0,
                quality=r["quality_score"] or 0, valuation=r["valuation_score"] or 0,
                dividend_total_return=r["dividend_total_return_score"] or 0,
                risk_penalty=r["risk_total_penalty"] or 0,
                price=r["current_price"], sma200=r["sma_200"], high_52w=r["high_52w"] or 0,
                momentum_12_1=r["momentum_12_1"]) for r in wl]
    ranked = cmp.rank_universe(names, CC.rank_fund_weight, CC.rank_mom_weight)
    green, yellow = [], []
    for rank, n in enumerate(ranked, 1):
        att = cmp.fair_price_attractiveness(n.price, n.sma200, n.high_52w)
        (yellow if att < 0 else green).append((rank, n.symbol, att))
    top_green = green[:N_EACH]
    top_yellow = yellow[:N_EACH]
    print(f"USD watchlist: {len(ranked)} names — {len(green)} green, {len(yellow)} yellow")
    print(f"top {N_EACH} GREEN : " + ", ".join(f"{s}({att:+.2f})" for _, s, att in top_green))
    print(f"top {N_EACH} YELLOW: " + ", ".join(f"{s}({att:+.2f})" for _, s, att in top_yellow))

    key = get_fmp_key()
    import datetime as _dt
    start = (_dt.date(2026, 7, 15) - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    def measure(group, label):
        drifts, per = [], []
        for _, sym, att in group:
            bars = fetch_ohlc(sym, start, key)[-10:]     # last 10 trading days
            time.sleep(0.12)
            if not bars:
                continue
            d = [(c / o - 1.0) for _, o, c in bars if o > 0]
            drifts.extend(d)
            per.append((sym, att, 100 * np.mean(d), len(d)))
        arr = np.array(drifts)
        print(f"\n### {label}: {len(arr)} name-days")
        if len(arr):
            print(f"  mean close/open drift: {100*arr.mean():+.3f}%   median {100*np.median(arr):+.3f}%   "
                  f"close<open on {100*np.mean(arr<0):.0f}% of days")
            print(f"  => buying LATE (close) vs OPEN would {'SAVE' if arr.mean()<0 else 'COST'} "
                  f"~{abs(100*arr.mean()):.3f}% per buy")
        for sym, att, m, nd in sorted(per, key=lambda x: x[2]):
            print(f"    {sym:<6} att {att:+.2f}  meanDrift {m:+.3f}%  ({nd}d)")
        return arr

    g = measure(top_green, "GREEN (below fair — dip names)")
    y = measure(top_yellow, "YELLOW (above fair — extended/momentum)")
    print("\n=== VERDICT ===")
    if len(g) and len(y):
        print(f"  green mean drift {100*g.mean():+.3f}%   yellow mean drift {100*y.mean():+.3f}%")
        if y.mean() > 0 and g.mean() < 0:
            print("  CONFIRMS Rain: green drifts DOWN (buy late), yellow drifts UP (buy early). "
                  "=> late-session change must be GREEN-ONLY.")
        elif y.mean() < 0:
            print("  Yellow ALSO drifts down — late-session helps both; no need to special-case yellow.")
        else:
            print("  Mixed — read per-name detail before deciding.")


if __name__ == "__main__":
    main()
