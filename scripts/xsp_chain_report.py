"""XSP dry-run chain report (2026-11-10): the strike sweep on REAL premiums.

Joins the `chain` rows (every scan: bid/ask per strike from the money to 3.5% OTM) with the
`settle` rows (next-morning settlement price per expiry) that the live dry run writes to
data/xsp_chain.jsonl, and prints per-OTM-bucket what a 1-contract short put would have earned:
premium (bid), breach rate, intrinsic paid, net, and the same for a put spread with the wing
`--wing` percent below (wing bought at ASK from the same chain row).

Usage:
    .venv/bin/python scripts/xsp_chain_report.py            # data/xsp_chain.jsonl
    .venv/bin/python scripts/xsp_chain_report.py --wing 0.03 --path data/xsp_chain.jsonl
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.index_puts import read_jsonl  # noqa: E402

BUCKETS = [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.5)]


def bucket_of(otm_pct: float):
    for lo, hi in BUCKETS:
        if lo <= otm_pct < hi:
            return f"{lo:.1f}-{hi:.1f}%"
    return None


def wing_ask_for(quotes: list[dict], short_strike: float, wing_pct: float):
    target = short_strike * (1 - wing_pct)
    cands = [q for q in quotes if q["strike"] <= target and q.get("ask")]
    if not cands:
        return None, None
    q = max(cands, key=lambda x: x["strike"])
    return q["strike"], q["ask"]


def summarize(rows: list[dict], wing_pct: float):
    settles = {r["expiry"]: r["settle_px"] for r in rows if r.get("type") == "settle"}
    chains = [r for r in rows if r.get("type") == "chain"]
    agg = defaultdict(lambda: {"n": 0, "prem": 0.0, "intr": 0.0, "breach": 0, "sp_n": 0, "sp_net": 0.0,
                               "sp_cost": 0.0, "sp_breach": 0, "notional": 0.0})
    scans_settled = 0; scans_open = 0
    for r in chains:
        px = settles.get(r["expiry"])
        if px is None:
            scans_open += 1
            continue
        scans_settled += 1
        for q in r["quotes"]:
            b = bucket_of(q["otm_pct"])
            if b is None or not q.get("bid"):
                continue
            k = q["strike"]; bid = q["bid"]
            intr = max(0.0, k - px)
            a = agg[b]
            a["n"] += 1; a["prem"] += bid; a["intr"] += intr; a["breach"] += int(px < k)
            a["notional"] += k
            wk, wa = wing_ask_for(r["quotes"], k, wing_pct)
            if wk is not None and wa is not None:
                net = bid - wa
                payoff = net - max(0.0, k - px) + max(0.0, wk - px)
                a["sp_n"] += 1; a["sp_net"] += payoff; a["sp_cost"] += wa; a["sp_breach"] += int(px < k)
    return agg, scans_settled, scans_open, len(settles)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="data/xsp_chain.jsonl")
    ap.add_argument("--wing", type=float, default=0.03)
    args = ap.parse_args()
    rows = read_jsonl(args.path)
    if not rows:
        print(f"no rows in {args.path}")
        return
    agg, n_settled, n_open, n_exp = summarize(rows, args.wing)
    print(f"{args.path}: {n_settled} settled scans, {n_open} awaiting settlement, {n_exp} expiries settled")
    print(f"\n  per-contract, 1-DTE short put, bid at scan; spread wing {args.wing:.0%} below at ask")
    print(f"  {'OTM bucket':11} {'n':>4} {'avg prem':>9} {'breach%':>8} {'avg intr':>9} {'net/ctr':>8} "
          f"{'yield/day':>9} | {'spr n':>5} {'wing cost':>9} {'spr net/ctr':>11}")
    for lo, hi in BUCKETS:
        b = f"{lo:.1f}-{hi:.1f}%"
        a = agg.get(b)
        if not a or a["n"] == 0:
            continue
        n = a["n"]; prem = a["prem"] / n; intr = a["intr"] / n; net = prem - intr
        notional = a["notional"] / n
        print(f"  {b:11} {n:>4d} {prem:>9.2f} {100 * a['breach'] / n:>7.0f}% {intr:>9.2f} {net:>8.2f} "
              f"{100 * net / notional:>8.3f}% | {a['sp_n']:>5d} "
              f"{(a['sp_cost'] / a['sp_n'] if a['sp_n'] else 0):>9.2f} "
              f"{(a['sp_net'] / a['sp_n'] if a['sp_n'] else 0):>11.2f}")
    print("\n  yield/day = net per contract / strike (× ~250 for a naive annual figure on 1× notional).")
    print("  Needs ≥ 15 settled scans before any bucket means anything; one −4% day moves every row.")


if __name__ == "__main__":
    main()
