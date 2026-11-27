"""1-DTE XSP put-spread (2 sigma short, 3% wing) on real SPY OHLC 2005-2026, stop evaluated on each day's LOW.
Flat-VIX BSM marks (checked vs the 11-27 live chain: 694P bid 0.48 vs BSM 0.46; wing ask ~2x BSM)."""
import json, math, sys, statistics as st
from datetime import date
S = sys.argv[1] if len(sys.argv) > 1 else "data"
ohlc = json.load(open(S + "/spy_ohlc_20y.json"))
vix = dict(json.load(open(S + "/spy_vix_20y.json"))["^VIX"])
days = sorted(d for d in ohlc if d in vix)
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
def put(Sp, K, iv, T):
    if T <= 0: return max(K - Sp, 0.0)
    sq = iv * math.sqrt(T)
    d1 = (math.log(Sp / K) + 0.5 * sq * sq) / sq; d2 = d1 - sq
    return K * N(-d2) - Sp * N(-d1)
def spread(Sp, K, W, iv, T, ask=False):
    v = put(Sp, K, iv, T) - put(Sp, W, iv, T) * (1.0 if ask else 1.8)
    return max(v, 0.0) * (1.05 if ask else 1.0)
NLV, MAXLOSS, COMM, VIXMAX, KSIG, FLOOR, WIDTH = 200_000.0, 0.05, 0.87, 35.0, 2.0, 0.015, 0.03
RULES = {"no stop": None, "3x credit (live)": ("m", 3), "5x credit": ("m", 5), "8x credit": ("m", 8),
         "floor 10% width": ("f", 0.10), "floor 15% width": ("f", 0.15), "floor 20% width": ("f", 0.20),
         "floor 25% width": ("f", 0.25), "floor 33% width": ("f", 0.33), "floor 50% width": ("f", 0.50)}
def level(rule, credit, width):
    if rule is None: return None
    kind, x = rule
    return x * credit if kind == "m" else max(3 * credit, x * width)
def run(d0, d1):
    idx = [i for i, d in enumerate(days) if d0 <= d <= d1 and i >= 21 and i + 1 < len(days)]
    res = {}
    for name, rule in RULES.items():
        pnl, eq, peak, dd, nstop, whip, saved, n = 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0
        for i in idx:
            d, dn = days[i], days[i + 1]
            o, h, l, c = ohlc[d]; o1, h1, l1, c1 = ohlc[dn]
            v_prev, v_d, v_n = vix[days[i - 1]], vix[d], vix[dn]
            if v_prev > VIXMAX: continue
            closes = [ohlc[days[j]][3] for j in range(i - 20, i)]
            sig = st.pstdev([math.log(closes[j] / closes[j - 1]) for j in range(1, 20)])
            Sp = o * 1.0                                    # XSP ~ SPY one-to-one
            K = Sp * (1 - max(FLOOR, KSIG * sig));  W = K * (1 - WIDTH)
            width = K - W; iv0 = v_prev / 100
            credit = put(Sp, K, iv0, 1.9 / 252) * 0.97 - put(Sp, W, iv0, 1.9 / 252) * 1.8 * 1.03
            if credit < 0.10 * Sp / 730: continue
            qty = int(MAXLOSS * NLV / ((width - credit) * 100))
            if qty < 1: continue
            n += 1
            settle = max(K - c1, 0) - max(W - c1, 0)
            lvl = level(rule, credit, width)
            paid = None
            if lvl is not None:
                iv_d = max(v_prev, v_d) / 100; iv_n = max(v_d, v_n) / 100
                if spread(l, K, W, iv_d, 1.5 / 252, True) >= lvl:  paid = lvl * 1.05 + 0.05
                else:
                    mo = spread(o1, K, W, iv_n, 0.9 / 252, True)
                    if mo >= lvl: paid = mo * 1.02 + 0.05
                    elif spread(l1, K, W, iv_n, 0.4 / 252, True) >= lvl: paid = lvl * 1.05 + 0.05
            if paid is not None:
                paid = min(paid, width)
                p = (credit - paid) * 100 * qty - COMM * 4 * qty; nstop += 1
                if settle == 0: whip += 1
                saved += (paid - settle) * 100 * qty if settle > paid else 0.0  # negative = stop cost vs holding
                saved -= (settle - paid) * 100 * qty if settle < paid else 0.0
            else:
                p = (credit - settle) * 100 * qty - COMM * 2 * qty
            pnl += p; eq += p; peak = max(peak, eq); dd = max(dd, peak - eq)
        yrs = max(len(idx) / 252, 1e-9)
        res[name] = dict(ret=pnl / NLV * 100 / yrs, dd=dd / NLV * 100, n=n, stops=nstop, whip=whip, saved=-saved / NLV * 100)
    return res
WINDOWS = [("all 2005-26", "2005-02-01", "2026-11-27"), ("gfc 2008 Sep-Dec", "2008-09-01", "2008-12-31"),
           ("volmageddon Feb18", "2018-01-20", "2018-03-10"), ("q4 2018", "2018-10-01", "2018-12-31"),
           ("covid 2020 Feb-Apr", "2020-02-15", "2020-04-30"), ("bear 2022", "2022-01-01", "2022-12-31"),
           ("tariff 2025 Mar-Apr", "2025-03-01", "2025-04-30"), ("bull 2021", "2021-01-01", "2021-12-31"),
           ("grind 2024 H1", "2024-01-01", "2024-06-30"), ("crash fortnight Nov26", "2026-11-09", "2026-11-27")]
print(f"{'window':24s} {'rule':18s} {'%/yr':>7s} {'maxDD%':>7s} {'n':>5s} {'stops':>5s} {'whips':>5s} {'stop vs hold %NLV':>18s}")
for w, a, b in WINDOWS:
    r = run(a, b)
    for name, x in r.items():
        print(f"{w:24s} {name:18s} {x['ret']:7.1f} {x['dd']:7.1f} {x['n']:5d} {x['stops']:5d} {x['whip']:5d} {x['saved']:18.2f}")
    print()
