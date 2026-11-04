"""Replay the account's REAL put fills under the 2026-11-04 rules (read-only).

For every FILLED SELL_PUT in data/options.db: the entry close (spot proxy), the close at
expiry (ITM or not), and what rules 3 (realised strike floor), 4 (breach gate) and 5 (10%
per-name cap) would have done with that fill. It cannot re-price the premium at a different
strike — only the loss side (ITM count, intrinsic paid) is exact. Daily closes come from
FMP via the project's own client (src/portfolio/fmp._get), 2019 → today.

    .venv/bin/python scripts/replay_real_puts.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.core.config import get_settings  # noqa: E402
from src.portfolio.fmp import _get  # noqa: E402
from src.strategy.realized_move import breach_rate, resolve_otm_window  # noqa: E402

DB = _ROOT / "data" / "options.db"
TRADES_DB = _ROOT / "data" / "trades.db"


def _closes(symbol: str, cache: dict) -> dict:
    if symbol in cache:
        return cache[symbol]
    data = _get("historical-price-eod/full", symbol, {"from": "2025-01-01", "to": date.today().isoformat()}) or []
    cache[symbol] = {r["date"]: float(r["close"]) for r in data if isinstance(r, dict) and r.get("close")}
    return cache[symbol]


def _close_on_or_before(series: dict, d: str):
    for i in range(7):
        dd = (date.fromisoformat(d) - timedelta(days=i)).isoformat()
        if dd in series:
            return series[dd]
    return None


def _closes_before(series: dict, d: str, n: int) -> list[float]:
    return [series[k] for k in sorted(series) if k < d][-n:]


def main():
    scfg = get_settings().strategy
    rcfg = get_settings().risk
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    puts = con.execute("select symbol, strike, expiry, premium, quantity, date(created_at) "
                       "from trades where trade_type='SELL_PUT' and order_status='FILLED' "
                       "order by created_at").fetchall()
    tcon = sqlite3.connect(f"file:{TRADES_DB}?mode=ro", uri=True)
    nlv = {d: v for d, v in tcon.execute("select date, net_liquidation from account_snapshots")}

    cache: dict = {}
    rows = []
    for sym, k, e, prem, q, d in puts:
        ser = _closes(sym, cache)
        if not ser:
            continue
        ed = f"{e[:4]}-{e[4:6]}-{e[6:]}"
        c0 = _close_on_or_before(ser, d)
        sc = _close_on_or_before(ser, ed)
        if not c0 or not sc:
            continue
        hist = _closes_before(ser, d, 130)
        floor, target, cap = resolve_otm_window(
            "realized", hist, flat_floor=scfg.put_otm_floor, flat_target=scfg.put_otm_target,
            flat_cap=scfg.put_otm_cap, k=scfg.put_otm_realized_k, lookback=scfg.put_otm_realized_lookback,
            pctl=scfg.put_otm_realized_pctl, horizon=scfg.put_otm_realized_horizon)
        br = breach_rate(hist, lookback=rcfg.put_universe_breach_lookback,
                         horizon=rcfg.put_universe_breach_horizon,
                         threshold=rcfg.put_universe_breach_threshold)
        otm_fill = 1 - k / c0
        nl = nlv.get(d) or 205_000.0
        rows.append({
            "symbol": sym, "date": d, "strike": k, "premium": prem, "qty": q, "spot": c0,
            "otm_fill": round(otm_fill, 4), "itm": sc < k, "intrinsic": max(0.0, k - sc) * q * 100,
            "prem_usd": prem * q * 100,
            "rule3_floor": floor, "rule3_blocks": otm_fill < floor,   # the fill sat inside the lifted floor
            "rule4_breach": br, "rule4_blocks": (br is not None and br > 0.30),
            "rule5_blocks": k * 100 > 0.20 * nl * 1.08,                # one lot over the 20% one-lot ceiling (USD≈EUR×1.08)
        })

    def table(label, keep):
        kept = [r for r in rows if keep(r)]
        skipped = [r for r in rows if not keep(r)]
        for name, grp in (("kept", kept), ("skipped", skipped)):
            n = len(grp)
            itm = sum(1 for r in grp if r["itm"])
            net = sum(r["prem_usd"] - r["intrinsic"] for r in grp)
            print(f"  {label:34} {name:8} n={n:3}  ITM {itm:3} ({(100 * itm / n if n else 0):4.0f}%)  "
                  f"premium−intrinsic ${net:>9,.0f}")

    print(f"Real put fills replayed: {len(rows)} of {len(puts)} (missing price history skipped)")
    print(f"actual: ITM {sum(1 for r in rows if r['itm'])}/{len(rows)}, premium ${sum(r['prem_usd'] for r in rows):,.0f}, "
          f"intrinsic ${sum(r['intrinsic'] for r in rows):,.0f}\n")
    table("rule 3 realized floor (fill inside)", lambda r: not r["rule3_blocks"])
    table("rule 4 breach ≤ 0.30", lambda r: not r["rule4_blocks"])
    table("rule 5 one lot ≤ 20% NLV", lambda r: not r["rule5_blocks"])
    table("rules 3+4+5 combined", lambda r: not (r["rule3_blocks"] or r["rule4_blocks"] or r["rule5_blocks"]))
    out = _ROOT / "data" / f"replay_real_puts_{date.today().strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(rows, indent=1, default=str))
    print(f"\nwrote {out}")
    print("NOTE: rule 3 'blocks' means the real strike sat closer than the lifted floor — live, the screen "
          "would have picked a farther strike (smaller premium, smaller intrinsic), not skipped the name.")


if __name__ == "__main__":
    main()
