#!/usr/bin/env python
"""READ-ONLY: what the compounder's entry pacing cost, in two framings.

    .venv/bin/python scripts/compounder_lump_vs_dca.py \\
        [--day1 YYYY-MM-DD] [--anchor2 YYYY-MM-DD] [--mode same-euros|full-deploy|both]

MODE 1 — `same-euros` (the original question): the same stocks, the same euros, ALL ON DAY 1.
Names and euros are held fixed and only the ENTRY DATE varies, so whatever is left over is the
cost (or the saving) of averaging in, and nothing else.

MODE 2 — `full-deploy` (added 2026-09-21 at Rain's ask): the WHOLE deposit base into the screened
top-N on day 1 — every euro, not just the ~35% that reached stocks. This varies the SIZE as well
as the date, so it scores the DCA and the cash park TOGETHER: was holding two-thirds in XEON
while averaging in the rest a mistake? Reports equal-weight, conviction-weighted and SPY at both
anchors, plus a survivorship control that adds back the names the last screen dropped.

`both` (default) runs the two in turn off a single price fetch.

Run from the repo root: src/portfolio/fx.py opens data/portfolio_account_cache.json relatively.
Places no orders, writes nothing, and connects to IBKR read-only on an unused clientId.

═══════════════════════════════════════════════════════════════════════════════════════════════
READ THIS BEFORE TRUSTING THE HEADLINE — the book is NOT a sample of the strategy
═══════════════════════════════════════════════════════════════════════════════════════════════
Caveat 1 applies to BOTH modes. Caveat 2 applies to MODE 1 ONLY — full-deploy buys the entire
screened universe rather than the half the buyer actually reached, so its SPY line is a fair
comparison where mode 1's is not. Mode 2 carries two biases of its OWN instead, both printed
with its output: survivorship (today's list applied to a June decision) and, for the
conviction-weighted row, lookahead (target_weights ranks on momentum read TODAY).

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
ACCOUNT = "U26413485"  # the compounder account; capital_injections also holds the options account
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


def load_watchlist():
    """The screened universe, split into current members and the names the last screen DROPPED.

    Drop-outs are `pending_removal` rows — still in the table, excluded from the live book. They
    matter here for one reason only: leaving them out is survivorship, and it flatters the lump
    (a name is usually dropped because it went wrong). full-deploy prices both baskets.
    """
    con = sqlite3.connect(DB, uri=True); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        """SELECT symbol, tier, exchange, currency, pending_removal,
                  growth_score, forward_growth_score, quality_score, valuation_score,
                  dividend_total_return_score, risk_total_penalty,
                  current_price, sma_200, high_52w, momentum_12_1
           FROM portfolio_watchlist WHERE symbol != ?""", (PARK,))]
    con.close()
    return ([r for r in rows if not r["pending_removal"]],
            [r for r in rows if r["pending_removal"]])


def load_capital():
    """(total deposits, {date: cumulative}, latest NLV row) for the compounder account.

    The deposit CURVE is the point: the account held EUR 10k on 2026-06-20 and did not hold the
    full balance until 2026-07-07. A day-1 lump of the whole base is therefore a pure-timing
    fiction, and the anchor2 run is the only one you could actually have placed.
    """
    con = sqlite3.connect(DB, uri=True); con.row_factory = sqlite3.Row
    dep = con.execute("""SELECT date, SUM(amount_original) amt FROM portfolio_capital_injections
                         WHERE account_id = ? GROUP BY date ORDER BY date""", (ACCOUNT,)).fetchall()
    nlv = dict(con.execute("""SELECT portfolio_nlv, date FROM account_snapshots
                              ORDER BY id DESC LIMIT 1""").fetchone())
    con.close()
    cum, curve = 0.0, {}
    for r in dep:
        cum += r["amt"] or 0.0
        curve[r["date"]] = cum
    return cum, curve, nlv


def load_signals():
    """The buyer's own last-scan ranking — tier targets and green/yellow, for the composition block."""
    import json
    con = sqlite3.connect(DB, uri=True)
    row = con.execute("SELECT value FROM portfolio_state WHERE key='compounder_signals'").fetchone()
    con.close()
    return json.loads(row[0]) if row else []


def duration_for(day1: str) -> str:
    """IBKR durationStr long enough to reach `day1`, with slack for weekends/holidays.

    This was a hard-coded "90 D", which silently stopped reaching 2026-06-20 once the window
    passed ~13 weeks: reqHistoricalData counts CALENDAR days, so the day-1 bar simply wasn't in
    the response and the `assert not missing` below fired with no hint as to why. Derive it.
    """
    from datetime import date
    y, m, d = (int(x) for x in day1.split("-"))
    span = (date.today() - date(y, m, d)).days
    return f"{max(90, span + 45)} D"


def _bars(ib, sym, ccy, exch, duration):
    c = Stock(sym, exch, ccy)
    try:
        if not ib.qualifyContracts(c):
            return None, "qualify failed"
        b = ib.reqHistoricalData(c, endDateTime="", durationStr=duration, barSizeSetting="1 day",
                                 whatToShow="TRADES", useRTH=True, formatDate=1)
        return (b, None) if b else (None, "no bars")
    except Exception as e:
        return None, str(e)[:60]


def fetch_closes(rows, duration="90 D"):
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
            bars, err = _bars(ib, sym, ccy, native, duration)
            route = native
            if bars is None and native != "SMART":
                bars, err2 = _bars(ib, sym, ccy, "SMART", duration)
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


def report_full_deploy(px, members, dropouts, day1, anchor2, base):
    """MODE 2: the WHOLE capital base into the top-N on day 1, vs what the account actually did.

    Mode 1 holds names and euros fixed and varies only the date, so it isolates DCA timing. This
    one varies the SIZE as well — it spends every deposited euro instead of the ~35% actually in
    stocks — so it answers the bigger question: was the DCA *and the cash park together* a
    mistake? Because it buys the entire universe rather than the green-selected half the buyer
    actually reached, the SPY line here is a fair comparison, which it is NOT in mode 1.

    Three weightings, because they fail differently:
      equal-weight      — only survivorship bias (which names are on today's list).
      conviction-weight — ALSO lookahead: target_weights ranks on momentum_12_1 read TODAY, so it
                          scores a June portfolio with knowledge of what happened since. It is a
                          biased-in-its-own-favour upper bound; if it loses to equal-weight, that
                          result is worth more than its margin suggests.
      SPY               — the index.
    """
    from src.core.config import get_settings
    from src.portfolio.compounder import (NameInput, rank_universe, target_weights,
                                          leader_symbols)
    cc = get_settings().portfolio.compounder
    total_dep, curve, nlv = load_capital()
    hold = load_holdings()
    rates = pfx.load_fx_rates()

    def deposited_by(day):
        return max([v for d, v in curve.items() if d <= day] or [0.0])

    def series(sym):
        return px.get(sym) or {}

    def last(sym):
        s = series(sym)
        return s[max(s)] if s else None

    def basket(syms, day, capital):
        """Buy `capital` spread over `syms` at `day`'s LOCAL close, value at the last close.

        FX is spot on both legs, exactly as mode 1 — the rate cancels, so this measures
        local-currency performance and says nothing about the euro cost of holding yen.
        """
        per = capital / len(syms)
        return basket_w({s: per for s in syms}, day)

    def basket_w(weights, day):
        cost = term = 0.0
        rows = []
        for s, eur in weights.items():
            p0, p1 = close_on(series(s), day), last(s)
            if not p0 or not p1:
                continue
            cost += eur; term += eur * (p1 / p0)
            rows.append((s, (p1 / p0 - 1) * 100, eur * (p1 / p0) - eur))
        return cost, term, rows

    # ── what the account actually did with the same euros ──
    stock = sum(pfx.to_base(h["market_value"] or 0.0, h["currency"], rates) for h in hold)
    cost_b = sum(pfx.to_base(h["total_invested"] or 0.0, h["currency"], rates) for h in hold)
    act_pnl = nlv["portfolio_nlv"] - total_dep

    print("\n" + "=" * 92)
    print(f"  FULL-DEPLOY ON DAY 1 vs ACTUAL  |  the entire {total_dep:,.0f} {base}, not just what was spent")
    print("=" * 92)
    print(f"  deposits {total_dep:>12,.0f}   NLV {nlv['date']} {nlv['portfolio_nlv']:>12,.0f}   "
          f"= {act_pnl:>+11,.0f}  ({act_pnl / total_dep * 100:+.2f}%)")
    print(f"    in stocks {stock:>11,.0f} ({stock / nlv['portfolio_nlv'] * 100:4.1f}% of NLV, "
          f"cost {cost_b:,.0f} -> {(stock / cost_b - 1) * 100:+.2f}%)")
    print(f"    parked / cash {nlv['portfolio_nlv'] - stock:>7,.0f} "
          f"({(nlv['portfolio_nlv'] - stock) / nlv['portfolio_nlv'] * 100:4.1f}%)  "
          f"<- this, not stock picking, is why the headline is near flat")

    names = [NameInput(w["symbol"], w["tier"], w["growth_score"] or 0,
                       w["forward_growth_score"] or 0, w["quality_score"] or 0,
                       w["valuation_score"] or 0, w["dividend_total_return_score"] or 0,
                       w["risk_total_penalty"] or 0, w["current_price"], w["sma_200"],
                       w["high_52w"], w["momentum_12_1"]) for w in members]
    ranked = rank_universe(names, cc.rank_fund_weight, cc.rank_mom_weight)
    live = [r.symbol for r in ranked if close_on(series(r.symbol), day1) and last(r.symbol)]
    surv = [w["symbol"] for w in dropouts
            if close_on(series(w["symbol"]), day1) and last(w["symbol"])]
    skipped = [r.symbol for r in ranked if r.symbol not in live]
    print(f"\n  universe: {len(live)} members priced on {day1} and today"
          f"{f' (no day-1 bar: {skipped})' if skipped else ''}")
    print(f"  drop-outs available for the survivorship control: {surv or 'none'}")

    cw = target_weights(ranked, {"breakthrough": cc.tier_breakthrough, "growth": cc.tier_growth,
                                 "dividend": cc.tier_dividend}, total_dep,
                        per_name_cap_pct=cc.per_name_cap_pct,
                        leader_syms=leader_symbols(ranked, cc.leader_top_frac),
                        leader_cap_pct=cc.leader_cap_pct,
                        conviction_power=cc.conviction_power,
                        abs_ceiling=cc.per_name_abs_ceiling)
    cw = {s: v for s, v in cw.items() if s in set(live)}

    for day, tag in ((day1, "day-1"), (anchor2, "capital-feasible")):
        have = deposited_by(day)
        print(f"\n  ANCHOR {day} ({tag}) — deposited by then: {have:,.0f} {base}"
              + ("   <- the whole base was NOT yet in the account" if have < total_dep * 0.95 else ""))
        print(f"  {'run':<38}{'terminal':>14}{'P&L':>13}{'return':>9}{'vs actual':>12}")
        runs = [(f"equal-weight top{len(live)}", basket(live, day, total_dep)),
                (f"conviction-weighted top{len(live)}", basket_w(cw, day)),
                (f"equal-weight top{len(live)} + {len(surv)} drop-outs",
                 basket(live + surv, day, total_dep)),
                ("SPY lump", basket(["SPY"], day, total_dep))]
        for label, (cost, term, _) in runs:
            print(f"  {label:<38}{term:>14,.0f}{term - cost:>+13,.0f}"
                  f"{(term / cost - 1) * 100:>8.2f}%{(term - cost) - act_pnl:>+12,.0f}")

    _, _, rows = basket(live, day1, total_dep)
    rows.sort(key=lambda r: -r[2])
    wins = sum(1 for r in rows if r[1] > 0)
    med = sorted(r[1] for r in rows)[len(rows) // 2]
    print(f"\n  equal-weight, {day1} anchor: {wins}/{len(rows)} names up, median {med:+.1f}%")
    print(f"  {'best':<8}{'move':>9}{'P&L':>11}     {'worst':<8}{'move':>9}{'P&L':>11}")
    for i in range(min(8, len(rows) // 2)):
        a, b = rows[i], rows[-(i + 1)]
        print(f"  {a[0]:<8}{a[1]:>+8.1f}%{a[2]:>11,.0f}     "
              f"{b[0]:<8}{b[1]:>+8.1f}%{b[2]:>11,.0f}")
    print(f"\n  Read the capital-feasible row, not the day-1 row: on {day1} the account held "
          f"{deposited_by(day1):,.0f} {base}.\n  Survivorship is only partly repaired — the "
          f"drop-out control covers names still in the table.\n  Names the June screen held that "
          f"never reached today's table are unrecoverable "
          f"(config/screened_universe.yaml\n  was untracked in 728695f), and those are the ones "
          f"most likely to have gone wrong.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day1", default="2026-06-20", help="compounder_start_date; a non-trading day rolls forward")
    ap.add_argument("--anchor2", default="2026-07-03", help="first day deposits covered the deployed sum")
    ap.add_argument("--mode", default="both", choices=("same-euros", "full-deploy", "both"),
                    help="same-euros: the held names, same euros, only the DATE differs. "
                         "full-deploy: the WHOLE deposit base into the screened top-N. "
                         "both (default): run each in turn.")
    a = ap.parse_args()

    rates = pfx.load_fx_rates(); base = pfx.base_ccy(rates)
    hold = load_holdings()
    sig = load_signals()
    held_syms = {h["symbol"] for h in hold}
    members, dropouts = load_watchlist() if a.mode in ("full-deploy", "both") else ([], [])

    print_composition(sig, held_syms)

    # One fetch feeds both modes. Dedupe on symbol: a held name is usually also a watchlist member,
    # and IBKR pacing is the slow part of this script.
    want, seen = [], set()
    for sym, ccy, exch in ([(h["symbol"], h["currency"], h["exchange"]) for h in hold]
                           + [(w["symbol"], w["currency"], w["exchange"])
                              for w in members + dropouts]
                           + [("SPY", "USD", "SMART")]):
        if sym not in seen:
            seen.add(sym); want.append((sym, ccy, exch))
    print(f"\nfetching daily bars for {len(want)} series "
          f"({len(hold)} held, {len(members)} members, {len(dropouts)} drop-outs, SPY) ...")
    px, how = fetch_closes(want, duration_for(a.day1))
    rerouted = sorted(s for s, r in how.items() if "(" in r)
    if rerouted:
        print(f"  routed via SMART after a native failure: {rerouted}")

    day1 = a.day1
    probe = close_on(px.get("SPY", {}), day1)
    resolved = next((d for d in sorted(px.get("SPY", {})) if d >= day1), day1)
    print(f"day 1 requested {day1} -> resolves to {resolved} (first trading day on/after); "
          f"SPY close {probe}")

    if a.mode in ("same-euros", "both"):
        report_same_euros(px, hold, rates, base, day1, resolved, a.anchor2)
    if a.mode in ("full-deploy", "both"):
        report_full_deploy(px, members, dropouts, resolved, a.anchor2, base)


def report_same_euros(px, hold, rates, base, day1, resolved, anchor2):
    """MODE 1 (the original): the held names and the euros actually spent, entered all on day 1.

    Names and euros are held FIXED and only the entry date varies, so the lump-minus-actual gap is
    a clean read on DCA timing. The SPY and equal-weight lines carry the caveat block above — the
    roster is the green-selected half of the universe, so they are not a verdict on selection.
    """
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
        p0, p0b = close_on(px[sym], day1), close_on(px[sym], anchor2)
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
    print(line(f"LUMP all-in {anchor2} (capital in)", tot_anchor2))
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

    early = [r for r in rows if (r["fb"] or "9999") <= anchor2]
    if early:
        eb = sum(r["basis"] for r in early)
        ea, el = sum(r["actual"] for r in early), sum(r["lump"] for r in early)
        print(f"\n  FORESIGHT CONTROL — only the {len(early)} names already bought by {anchor2}.")
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
