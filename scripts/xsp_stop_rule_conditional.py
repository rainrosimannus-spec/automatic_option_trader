import sys, math, statistics as st
S = sys.argv[1] if len(sys.argv) > 1 else "data"
src = open("scripts/xsp_stop_rule_model.py").read().split("WINDOWS = [")[0]
sys.argv = ["x", S]; g = {}; exec(src, g)
days, ohlc, vix, put, spread = g["days"], g["ohlc"], g["vix"], g["put"], g["spread"]
NLV, MAXLOSS, COMM, VIXMAX, KSIG, FLOOR, WIDTH = 200_000.0, 0.05, 0.87, 35.0, 2.0, 0.015, 0.03
closes = {d: ohlc[d][3] for d in days}
def ma(i, n): return sum(closes[days[j]] for j in range(i - n, i)) / n
def vix_ma(i, n): return sum(vix[days[j]] for j in range(i - n, i)) / n
# condition(i) -> stop ON for the spread sold on day i (uses only data known before the open of day i)
CONDS = {
  "never (no stop)": lambda i: False,
  "always 3x (live)": lambda i: True,
  "VIX>18": lambda i: vix[days[i-1]] > 18,
  "VIX>20": lambda i: vix[days[i-1]] > 20,
  "VIX>25": lambda i: vix[days[i-1]] > 25,
  "VIX>30": lambda i: vix[days[i-1]] > 30,
  "SPY<MA200": lambda i: closes[days[i-1]] < ma(i, 200),
  "SPY<MA50": lambda i: closes[days[i-1]] < ma(i, 50),
  "SPY<MA200 & VIX>20": lambda i: closes[days[i-1]] < ma(i, 200) and vix[days[i-1]] > 20,
  "VIX>1.3x its 20d avg": lambda i: vix[days[i-1]] > 1.3 * vix_ma(i, 20),
  "VIX>25 or VIX>1.3x avg": lambda i: vix[days[i-1]] > 25 or vix[days[i-1]] > 1.3 * vix_ma(i, 20),
  "SPY 5-day drop >3%": lambda i: closes[days[i-1]] / closes[days[i-6]] - 1 < -0.03,
  "SPY >5% off 20d high": lambda i: closes[days[i-1]] < 0.95 * max(closes[days[j]] for j in range(i-20, i)),
}
def run(d0, d1):
    idx = [i for i, d in enumerate(days) if d0 <= d <= d1 and i >= 201 and i + 1 < len(days)]
    res = {}
    for name, cond in CONDS.items():
        pnl, eq, peak, dd, nstop, whip, on = 0.0, 0.0, 0.0, 0.0, 0, 0, 0
        for i in idx:
            d, dn = days[i], days[i + 1]
            o, h, l, c = ohlc[d]; o1, h1, l1, c1 = ohlc[dn]
            v_prev, v_d, v_n = vix[days[i - 1]], vix[d], vix[dn]
            if v_prev > VIXMAX: continue
            cl = [ohlc[days[j]][3] for j in range(i - 20, i)]
            sig = st.pstdev([math.log(cl[j] / cl[j - 1]) for j in range(1, 20)])
            Sp = o; K = Sp * (1 - max(FLOOR, KSIG * sig)); W = K * (1 - WIDTH); width = K - W; iv0 = v_prev / 100
            credit = put(Sp, K, iv0, 1.9/252) * 0.97 - put(Sp, W, iv0, 1.9/252) * 1.8 * 1.03
            if credit < 0.10 * Sp / 730: continue
            qty = int(MAXLOSS * NLV / ((width - credit) * 100))
            if qty < 1: continue
            settle = max(K - c1, 0) - max(W - c1, 0)
            paid = None
            if cond(i):
                on += 1; lvl = 3 * credit
                iv_d = max(v_prev, v_d) / 100; iv_n = max(v_d, v_n) / 100
                if spread(l, K, W, iv_d, 1.5/252, True) >= lvl: paid = lvl * 1.05 + 0.05
                else:
                    mo = spread(o1, K, W, iv_n, 0.9/252, True)
                    if mo >= lvl: paid = mo * 1.02 + 0.05
                    elif spread(l1, K, W, iv_n, 0.4/252, True) >= lvl: paid = lvl * 1.05 + 0.05
            if paid is not None:
                paid = min(paid, width); p = (credit - paid) * 100 * qty - COMM * 4 * qty; nstop += 1
                if settle == 0: whip += 1
            else:
                p = (credit - settle) * 100 * qty - COMM * 2 * qty
            pnl += p; eq += p; peak = max(peak, eq); dd = max(dd, peak - eq)
        yrs = max(len(idx) / 252, 1e-9)
        res[name] = (pnl / NLV * 100 / yrs, dd / NLV * 100, on, nstop, whip)
    return res
WINDOWS = [("all 2005-26", "2005-11-01", "2026-11-27"), ("gfc 2008 Sep-Dec", "2008-09-01", "2008-12-31"),
           ("volmageddon Feb18", "2018-01-20", "2018-03-10"), ("q4 2018", "2018-10-01", "2018-12-31"),
           ("covid 2020 Feb-Apr", "2020-02-15", "2020-04-30"), ("bear 2022", "2022-01-01", "2022-12-31"),
           ("tariff 2025 Mar-Apr", "2025-03-01", "2025-04-30"), ("bull 2021", "2021-01-01", "2021-12-31"),
           ("grind 2024 H1", "2024-01-01", "2024-06-30"), ("crash fortnight Nov26", "2026-11-09", "2026-11-27")]
R = {w: run(a, b) for w, a, b in WINDOWS}
print(f"{'condition':24s}" + "".join(f"{w[:10]:>11s}" for w in R) + "   | all: maxDD  on stops whips")
for name in CONDS:
    print(f"{name:24s}" + "".join(f"{R[w][name][0]:11.1f}" for w in R) + f"   | {R['all 2005-26'][name][1]:6.1f} {R['all 2005-26'][name][2]:5d} {R['all 2005-26'][name][3]:5d} {R['all 2005-26'][name][4]:5d}")
