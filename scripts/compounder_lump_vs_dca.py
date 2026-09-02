#!/usr/bin/env python
"""READ-ONLY: what if the compounder had bought the same stocks, with the same euros, ALL ON DAY 1?

Same money, same names, same terminal prices — only the ENTRY DATE differs. Anything left over is
the cost (or the saving) of averaging in.

    .venv/bin/python scripts/compounder_lump_vs_dca.py [--day1 YYYY-MM-DD] [--anchor2 YYYY-MM-DD]

Run from the repo root: src/portfolio/fx.py opens data/portfolio_account_cache.json relatively.
Places no orders, writes nothing, and connects to IBKR read-only on an unused clientId.

═══════════════════════════════════════════════════════════════════════════════════════════════
READ THIS BEFORE TRUSTING THE HEADLINE — the book is NOT a sample of the strategy
═══════════════════════════════════════════════════════════════════════════════════════════════
The compounder is ~30% deployed, and what it has bought so far is heavily selected in two ways
that both bias this comparison. The COMPOSITION block below prints the live figures every run;
read it first, because the headline means different things at 30% deployed and at 90%.

  1. BREAKTHROUGH FIRST. Tier fill is wildly uneven (2026-09-02: breakthrough 50% of target,
     growth 22%, dividend 7%). Breakthrough was ~51% of the invested book against ~30% of the
     eventual target — roughly 1.7x overweight versus where the strategy is heading. Those are
     the highest-beta names in the universe, so the book swings far harder than the finished
     portfolio will. Any comparison against SPY, or any claim about "the sizing", is really a
     statement about the breakthrough tail, not about the compounder.

  2. GREEN ONLY, SO THE BOOK IS THE LAGGARDS. A yellow name (above fair value) is never bought
     while any green name is still underweight. So the roster is, by construction, the names
     that had FALLEN to or below fair value — and in a rising market that is the losing half of
     the universe. 24 yellow names holding EUR 2.2M of target were still unbought on 2026-09-02.
     The book cannot beat an index it is selected to lag.

What survives those caveats: the lump-vs-DCA gap itself, because both runs hold the names and
the euros FIXED and vary only the date. What does NOT survive: reading the absolute return, the
SPY gap, or the equal-weight gap as a verdict on stock selection or on conviction sizing.
═══════════════════════════════════════════════════════════════════════════════════════════════

Provenance: extends live_deploy_vs_day1.py (2026-08-25 scratchpad). That version answered on the
US-only subset present in a since-stale price cache and excluded 6146/6920 as a then-live ~200x
price bug — both of which would have flipped the sign here, since the Tokyo pair are the biggest
movers in the book. Fixed by pulling prices from IBKR for all 40 names.
"""
import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ib_insync import IB, Stock                                    # noqa: E402
from src.core.quote_units import quote_to_major                    # noqa: E402
from src.portfolio import fx as pfx                                # noqa: E402

DB = "file:data/trades.db?mode=ro"
PARK = "XEON"          # cash-park ETF, never a compounder growth buy
CLIENT_ID = 161        # the live app holds 97; anything unused is fine


# ── data ────────────────────────────────────────────────────────────────────

def load_holdings():
    con = sqlite3.connect(DB, uri=True); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        """SELECT symbol, currency, exchange, tier, shares, avg_cost, total_invested,
                  current_price, market_value, total_dividends,
                  substr(first_bought, 1, 10) fb
           FROM portfolio_holdings WHERE shares > 0 AND symbol != ?""", (PARK,))]
    con.close()
    return rows


def load_signals():
    """The buyer's own last-scan ranking — tier targets and green/yellow, for the composition block."""
    import json
    con = sqlite3.connect(DB, uri=True)
    row = con.execute("SELECT value FROM portfolio_state WHERE key='compounder_signals'").fetchone()
    con.close()
    return json.loads(row[0]) if row else []


def _bars(ib, sym, ccy, exch):
    c = Stock(sym, exch, ccy)
    try:
        if not ib.qualifyContracts(c):
            return None, "qualify failed"
        b = ib.reqHistoricalData(c, endDateTime="", durationStr="90 D", barSizeSetting="1 day",
                                 whatToShow="TRADES", useRTH=True, formatDate=1)
        return (b, None) if b else (None, "no bars")
    except Exception as e:
        return None, str(e)[:60]


def fetch_closes(rows):
    """{symbol: {date: close in MAJOR units}} from IBKR daily bars.

    NATIVE exchange first, SMART only as a fallback. Neither alone works, and the failure is
    silent-ish if you get it wrong:
      * TSEJ (6920/6146) and TSE (CNQ/SU) have no historical market-data permission and answer
        Error 162 natively — routed via SMART the identical contracts return a full 90 bars.
      * SEHK (2318) is the mirror image: fine natively, but SMART cannot resolve the contract at
        all (Error 200, no security definition).
    A blanket reroute breaks Hong Kong exactly as a blanket native fetch breaks Tokyo/Toronto.
    """
    ib = IB(); ib.connect("127.0.0.1", 7496, clientId=CLIENT_ID, timeout=30, readonly=True)
    out, how = {}, {}
    try:
        for sym, ccy, exch in rows:
            native = exch or "SMART"
            bars, err = _bars(ib, sym, ccy, native)
            route = native
            if bars is None and native != "SMART":
                bars, err2 = _bars(ib, sym, ccy, "SMART")
                route, err = (f"SMART (native {native}: {err})", None) if bars is not None \
                    else (route, f"{native}: {err} | SMART: {err2}")
            if bars is None:
                print(f"  !! {sym}: {err}"); continue
            # IBKR quotes LSE in pence; the DB stores major units, so normalise at ingest.
            out[sym] = {str(b.date): quote_to_major(b.close, ccy) for b in bars}
            how[sym] = route
            time.sleep(0.12)
    finally:
        ib.disconnect()
    return out, how


def close_on(series, day):
    """Close on `day`, else the first trading day AFTER it — you cannot buy on a shut market."""
    for d in sorted(series):
        if d >= day:
            return series[d]
    return None


# ── report ──────────────────────────────────────────────────────────────────

def print_composition(sig, held_syms):
    """Why the headline is not a verdict on the strategy. Printed FIRST, on purpose."""
    if not sig:
        print("  (no compounder_signals in portfolio_state — run a scan first)"); return
    agg = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for r in sig:
        a = agg[r["tier"]]
        a[0] += 1; a[1] += r["symbol"] in held_syms
        a[2] += r.get("target", 0.0); a[3] += r.get("current", 0.0)
    print("=" * 92)
    print("  COMPOSITION — read before the headline")
    print("=" * 92)
    print(f"  {'tier':14s} {'names':>6s} {'held':>5s} {'target':>12s} {'invested':>12s} "
          f"{'filled':>7s} {'% of book':>10s} {'% of tgt':>9s}")
    inv_tot = sum(a[3] for a in agg.values()) or 1.0
    tgt_tot = sum(a[2] for a in agg.values()) or 1.0
    for t in ("breakthrough", "growth", "dividend"):
        n, h, tg, cu = agg.get(t, [0, 0, 0.0, 0.0])
        print(f"  {t:14s} {n:6d} {h:5d} {tg:12,.0f} {cu:12,.0f} "
              f"{(cu / tg * 100 if tg else 0):6.1f}% {cu / inv_tot * 100:9.1f}% {tg / tgt_tot * 100:8.1f}%")
    print(f"  {'TOTAL':14s} {sum(a[0] for a in agg.values()):6d} {len(held_syms):5d} "
          f"{tgt_tot:12,.0f} {inv_tot:12,.0f} {inv_tot / tgt_tot * 100:6.1f}%")
    gy = defaultdict(lambda: [0, 0.0])
    for r in sig:
        k = ("green" if r.get("attractiveness", 0) >= 0 else "yellow",
             "held" if r["symbol"] in held_syms else "NOT held")
        gy[k][0] += 1; gy[k][1] += r.get("target", 0.0)
    print("\n  green (at/below fair value) is bought first; yellow waits for the whole green list:")
    for k in (("green", "held"), ("green", "NOT held"), ("yellow", "held"), ("yellow", "NOT held")):
        n, tg = gy[k]
        print(f"    {k[0]:6s} {k[1]:9s}: {n:3d} names, target {tg:12,.0f}")
    print("\n  => the roster is the BREAKTHROUGH tail of the GREEN half of the universe.")
    print("     The lump-vs-DCA gap is still valid (names and euros are held fixed); the absolute")
    print("     return and the SPY/equal-weight gaps are NOT a verdict on selection or sizing.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day1", default="2026-06-20", help="compounder_start_date; a non-trading day rolls forward")
    ap.add_argument("--anchor2", default="2026-07-03", help="first day deposits covered the deployed sum")
    a = ap.parse_args()

    rates = pfx.load_fx_rates(); base = pfx.base_ccy(rates)
    hold = load_holdings()
    sig = load_signals()
    held_syms = {h["symbol"] for h in hold}

    print_composition(sig, held_syms)

    print(f"\nfetching daily bars for {len(hold)} holdings + SPY ...")
    px, how = fetch_closes([(h["symbol"], h["currency"], h["exchange"]) for h in hold]
                           + [("SPY", "USD", "SMART")])
    rerouted = sorted(s for s, r in how.items() if "(" in r)
    if rerouted:
        print(f"  routed via SMART after a native failure: {rerouted}")

    day1 = a.day1
    probe = close_on(px.get("SPY", {}), day1)
    resolved = next((d for d in sorted(px.get("SPY", {})) if d >= day1), day1)
    print(f"day 1 requested {day1} -> resolves to {resolved} (first trading day on/after); "
          f"SPY close {probe}")

    # guards — a silently dropped name would corrupt every total below
    missing = [h["symbol"] for h in hold
               if h["symbol"] not in px or close_on(px[h["symbol"]], day1) is None]
    assert not missing, f"no day-1 price for {missing}"
    azn = close_on(px.get("AZN", {}), day1)
    assert azn is None or 50 < azn < 500, f"AZN day-1 {azn} looks like pence, not pounds"
    for h in hold:
        assert pfx.has_rate(h["currency"], rates), f"no FX rate for {h['currency']}"

    rows = []
    tot_basis = tot_actual = tot_lump = tot_anchor2 = 0.0
    for h in hold:
        sym, ccy = h["symbol"], h["currency"]
        basis_l, p_now = h["total_invested"] or 0.0, h["current_price"] or 0.0
        p0, p0b = close_on(px[sym], day1), close_on(px[sym], a.anchor2)
        if basis_l <= 0 or p_now <= 0 or not p0:
            print(f"  !! {sym}: skipped (basis={basis_l} price={p_now} p0={p0})"); continue
        basis_e = pfx.to_base(basis_l, ccy, rates)
        actual_e = pfx.to_base(h["market_value"] or 0.0, ccy, rates)
        lump_e = pfx.to_base(basis_l * (p_now / p0), ccy, rates)
        tot_basis += basis_e; tot_actual += actual_e; tot_lump += lump_e
        tot_anchor2 += pfx.to_base(basis_l * (p_now / p0b), ccy, rates) if p0b else lump_e
        rows.append(dict(sym=sym, ccy=ccy, tier=h["tier"], basis=basis_e, actual=actual_e,
                         lump=lump_e, p0=p0, p1=p_now, diff=lump_e - actual_e, fb=h["fb"],
                         # what DCA paid per share vs the day-1 close: THE causal quantity
                         prem=(h["avg_cost"] or 0.0) / p0 - 1.0))

    n = len(rows)
    spy0, spy1 = close_on(px["SPY"], day1), px["SPY"][max(px["SPY"])]
    spy_e = tot_basis * (spy1 / spy0)
    eq_e = sum((tot_basis / n) * (r["p1"] / r["p0"]) for r in rows)

    def line(name, val):
        return (f"  {name:36s} {tot_basis:>12,.0f} {val:>13,.0f} {val - tot_basis:>+12,.0f} "
                f"{(val / tot_basis - 1) * 100:>+8.2f}%")

    print("\n" + "=" * 92)
    print(f"  LUMP ON DAY 1 vs ACTUAL DCA  |  {n} names, {base} base, spot FX held constant both runs")
    print("=" * 92)
    print(f"  {'run':36s} {'cost basis':>12s} {'value now':>13s} {'P&L':>12s} {'return':>9s}")
    print("  " + "-" * 88)
    print(line("ACTUAL (as deployed)", tot_actual))
    print(line(f"LUMP all-in {resolved}", tot_lump))
    print(line(f"LUMP all-in {a.anchor2} (capital in)", tot_anchor2))
    print(line(f"SPY lump {resolved} [caveats]", spy_e))
    print(line(f"equal-weight lump {resolved} [caveats]", eq_e))
    print("  " + "-" * 88)
    gap = tot_lump - tot_actual
    assert abs(sum(r["diff"] for r in rows) - gap) < 1.0, "per-name diffs do not sum to the gap"
    print(f"\n  LUMP minus ACTUAL: {gap:+,.0f} {base}  "
          f"({(tot_lump - tot_actual) / tot_basis * 100:+.2f} pp)")
    print(f"  -> averaging in {'COST' if gap > 0 else 'SAVED'} {abs(gap):,.0f} {base} "
          f"versus buying it all on day 1.")

    wprem = sum(r["prem"] * r["basis"] for r in rows) / tot_basis
    print(f"\n  cost-weighted entry premium (what DCA paid per share vs the {resolved} close): "
          f"{wprem * 100:+.1f}%")
    print(f"  names where DCA got a BETTER average price than day 1: "
          f"{sum(1 for r in rows if r['prem'] < 0)}/{n}")

    early = [r for r in rows if (r["fb"] or "9999") <= a.anchor2]
    if early:
        eb = sum(r["basis"] for r in early)
        ea, el = sum(r["actual"] for r in early), sum(r["lump"] for r in early)
        print(f"\n  FORESIGHT CONTROL — only the {len(early)} names already bought by {a.anchor2}.")
        print(f"  Back-dating a name the screener picked in August grants the lump run a pick it")
        print(f"  could not have made on day 1; this subset removes that advantage:")
        print(f"    basis {eb:>12,.0f}  actual {ea:>12,.0f} ({(ea / eb - 1) * 100:+.2f}%)  "
              f"lump {el:>12,.0f} ({(el / eb - 1) * 100:+.2f}%)  diff {el - ea:>+12,.0f}")

    rows.sort(key=lambda r: r["diff"])
    print("\n  per name, worst for the lump first (diff = lump value - actual value):")
    print(f"  {'sym':6s} {'ccy':4s} {'tier':13s} {'basis':>10s} {'day1 px':>10s} {'now px':>10s} "
          f"{'move':>8s} {'actual':>10s} {'lump':>10s} {'diff':>10s}")
    for r in rows[:8] + [None] + rows[-8:]:
        if r is None:
            print(f"  {'...':>6s}"); continue
        print(f"  {r['sym']:6s} {r['ccy']:4s} {r['tier']:13s} {r['basis']:>10,.0f} "
              f"{r['p0']:>10,.2f} {r['p1']:>10,.2f} {(r['p1'] / r['p0'] - 1) * 100:>+7.1f}% "
              f"{r['actual']:>10,.0f} {r['lump']:>10,.0f} {r['diff']:>+10,.0f}")

    div = sum(pfx.to_base(h.get("total_dividends") or 0.0, h["currency"], rates) for h in hold)
    print(f"\n  Price-only on both sides; dividends booked so far {div:,.0f} {base}. A lump held "
          f"from {resolved}\n  would have collected somewhat more, so the lump result is if "
          f"anything flattered, not penalised.\n  FX is spot on both runs, so the rate cancels: "
          f"this measures LOCAL-currency timing, not the\n  euro cost of having held yen or "
          f"sterling earlier.")


if __name__ == "__main__":
    main()
