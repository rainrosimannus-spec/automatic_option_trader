"""Point-in-time universe for MarsWalk (2026-11-24 fidelity audit, Rain).

The live options universe (config/options_universe.yaml) is rewritten by the monthly screener and had
no history before data/universe_history/ started on 2026-11-24, so every replay ran the PAST on
TODAY's list — the 2026 screener's winners. This module builds a universe that uses only
information available on each date:

  membership  = S&P 500 ∪ Nasdaq-100 as of the date (FMP historical constituent change logs)
  rank        = 0.60 × pct-rank(trailing 12-month return) + 0.40 × pct-rank(20-day dollar volume)
                — the live options_score is 0.60 × growth composite + 0.40 × options liquidity;
                momentum and dollar volume are the price-only proxies for those two legs
  universe    = top N (default 50) ranked names, price ≥ min_price, rebalanced on the first
                trading day of each month; the rank order is the live scan order

It is a PROXY for the screener (no fundamentals, no AI), stated as such wherever it is used.
Prices live in data/marswalk_pit.db (pit_bars: symbol, date, close, volume), constituents in
data/marswalk_pit/constituents.json. Nothing here touches the trading path.
"""
from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

PIT_DB = Path("data/marswalk_pit.db")
CONSTITUENTS = Path("data/marswalk_pit/constituents.json")
IV_WINDOW = 20          # same realised-vol proxy as marswalk.data.fetch_symbols_yahoo


# ── constituents ─────────────────────────────────────────────────────────────────────────────

def fetch_constituents(path: Path = CONSTITUENTS) -> dict:
    """FMP current lists + change logs for the S&P 500 and Nasdaq-100 → JSON cache."""
    import requests
    from src.portfolio.fmp import get_fmp_key, FMP_BASE
    key = get_fmp_key()
    out = {}
    for idx, cur, hist in (("sp500", "sp500-constituent", "historical-sp500-constituent"),
                           ("ndx", "nasdaq-constituent", "historical-nasdaq-constituent")):
        c = requests.get(f"{FMP_BASE}/{cur}", params={"apikey": key}, timeout=30).json()
        h = requests.get(f"{FMP_BASE}/{hist}", params={"apikey": key}, timeout=30).json()
        out[idx] = {"current": [r["symbol"] for r in c],
                    "changes": [{"date": r.get("date"), "added": r.get("addedTicker") or r.get("symbol"),
                                 "removed": r.get("removedTicker")} for r in h if r.get("date")]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    return out


def load_constituents(path: Path = CONSTITUENTS) -> dict:
    return json.loads(path.read_text())


def members_asof(cons: dict, d: str | date) -> set[str]:
    """Walk the change log backwards from today's list: undo every change dated after `d`."""
    ds = d.isoformat() if isinstance(d, date) else str(d)[:10]
    members: set[str] = set()
    for idx in cons.values():
        m = set(idx["current"])
        for ch in sorted(idx["changes"], key=lambda r: r["date"], reverse=True):
            if ch["date"] <= ds:
                break
            if ch.get("added"):
                m.discard(ch["added"])
            if ch.get("removed"):
                m.add(ch["removed"])
        members |= m
    return members


# ── prices ───────────────────────────────────────────────────────────────────────────────────

def load_bars(symbols: Iterable[str], db_path: Path = PIT_DB) -> dict[str, list[tuple[str, float, float]]]:
    """{symbol: [(date, close, volume), ...] oldest→newest} for the symbols present in the store."""
    out: dict[str, list] = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for s in symbols:
            rows = con.execute("select date, close, volume from pit_bars where symbol=? order by date", (s,)).fetchall()
            if rows:
                out[s] = [(r[0], float(r[1]), float(r[2] or 0.0)) for r in rows]
    finally:
        con.close()
    return out


def _pct_rank(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    return {k: (i / (n - 1) if n > 1 else 1.0) for i, (k, _) in enumerate(ordered)}


def screen_asof(bars: dict, members: set[str], d: str, n: int = 50, min_price: float = 30.0,
                lookback_days: int = 252, vol_days: int = 20,
                w_momentum: float = 0.60, w_liquidity: float = 0.40) -> list[str]:
    """Top-`n` members on day `d` by 0.6 × rank(12m return) + 0.4 × rank(dollar volume), using
    bars dated ≤ d only. Returns symbols in rank order (best first)."""
    mom: dict[str, float] = {}
    liq: dict[str, float] = {}
    for s in sorted(members):          # deterministic: `members` is a set (hash order varies per process)
        b = bars.get(s)
        if not b:
            continue
        dates = [x[0] for x in b]
        i = bisect_right(dates, d) - 1
        if i < lookback_days:
            continue
        px = b[i][1]
        if px < min_price:
            continue
        past = b[i - lookback_days][1]
        if past <= 0:
            continue
        mom[s] = px / past - 1.0
        liq[s] = sum(x[1] * x[2] for x in b[i - vol_days + 1:i + 1]) / vol_days
    rm, rl = _pct_rank(mom), _pct_rank(liq)
    score = {s: w_momentum * rm[s] + w_liquidity * rl.get(s, 0.0) for s in mom}
    # ties broken by symbol so two processes rank identically (2026-11-25: bear_2022 swung 35-43%
    # between sweeps on nothing but set iteration order)
    return [s for s, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def month_starts(start: str, end: str, trading_dates: list[str]) -> list[str]:
    """First trading date of each month in [start, end], plus `start` itself."""
    out = []
    seen = set()
    for ds in trading_dates:
        if ds < start or ds > end:
            continue
        key = ds[:7]
        if key not in seen:
            seen.add(key)
            out.append(ds)
    if trading_dates and start not in out:
        first = [ds for ds in trading_dates if ds >= start]
        if first and first[0] not in out:
            out.insert(0, first[0])
    return sorted(set(out))


def build_schedule(start: str, end: str, n: int = 50, cons: dict | None = None,
                   bars: dict | None = None, db_path: Path = PIT_DB, **screen_kw) -> list[tuple[date, list[str]]]:
    """[(rebalance_date, [symbols in rank order]), ...] for the engine's `universe_schedule`."""
    cons = cons or load_constituents()
    # names ever a member in the window (monthly samples are enough: changes are sparse)
    names: set[str] = set()
    d = datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=400)
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= end_d:
        names |= members_asof(cons, d)
        d += timedelta(days=30)
    bars = bars if bars is not None else load_bars(names, db_path)
    all_dates = sorted({x[0] for b in bars.values() for x in b})
    sched = []
    for ds in month_starts(start, end, all_dates):
        picks = screen_asof(bars, members_asof(cons, ds), ds, n=n, **screen_kw)
        if picks:
            sched.append((datetime.strptime(ds, "%Y-%m-%d").date(), picks))
    return sched


def market_from_bars(bars: dict, symbols: Iterable[str], start: str, end: str, warmup_days: int = 300
                     ) -> dict[str, list[tuple[date, float, float]]]:
    """Engine `market` entries {sym: [(date, close, iv)]} with the same trailing-20d realised-vol IV
    proxy as marswalk.data, plus `_pre:<sym>` warm-up bars (close only) for the MA200 builder."""
    s_d = datetime.strptime(start, "%Y-%m-%d").date()
    e_d = datetime.strptime(end, "%Y-%m-%d").date()
    pre_d = s_d - timedelta(days=warmup_days)
    out: dict[str, list] = {}
    for s in symbols:
        b = bars.get(s)
        if not b:
            continue
        rows, pre = [], []
        closes = [(datetime.strptime(x[0], "%Y-%m-%d").date(), x[1]) for x in b]
        for i, (d, c) in enumerate(closes):
            if d > e_d or d < pre_d:
                continue
            if d < s_d:
                pre.append((d, c, 0.0))
                continue
            if i < IV_WINDOW:
                continue
            rets = [math.log(closes[j + 1][1] / closes[j][1]) for j in range(i - IV_WINDOW, i)
                    if closes[j][1] > 0 and closes[j + 1][1] > 0]
            if len(rets) < 5:
                continue
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / max(1, len(rets) - 1)
            iv = math.sqrt(var) * math.sqrt(252)
            if iv > 0:
                rows.append((d, c, iv))
        if rows:
            out[s] = rows
        if pre:
            out[f"_pre:{s}"] = pre
    return out


def pit_available(db_path: Path = PIT_DB, cons_path: Path = CONSTITUENTS) -> bool:
    return db_path.exists() and cons_path.exists()


def regime_inputs(regime, n: int = 50, db_path: Path = PIT_DB) -> tuple[list[str], dict, list]:
    """(names, market, schedule) for `regime` on the point-in-time universe. `market` carries the
    PIT names (+ warm-up) only; the caller adds the regime's ^VIX/^SPY/^IDX bars."""
    start, end, _ = regime.effective_window()
    cons = load_constituents()
    sched = build_schedule(start, end, n=n, cons=cons, db_path=db_path)
    names = sorted({sym for _, picks in sched for sym in picks})
    bars = load_bars(names, db_path)
    market = market_from_bars(bars, names, start, end)
    return names, market, sched

# ── live refresh (shadow selector, 2026-11-25) ───────────────────────────────────────────────

def latest_bar_date(db_path: Path = PIT_DB) -> str | None:
    """Newest bar date in the store (None when empty/missing)."""
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute("select max(date) from pit_bars").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def refresh_recent(days: int = 400, db_path: Path = PIT_DB, cons_path: Path = CONSTITUENTS,
                   sleep_s: float = 0.05, fetch=None) -> dict:
    """Refresh the constituent lists and the last `days` of daily bars for every CURRENT
    S&P 500 ∪ Nasdaq-100 member (FMP, ~600 calls, a few minutes). Upserts, so history stays.
    `fetch(symbol, from_iso, to_iso) -> list[{"date","close","volume"}]` is injectable for tests."""
    import time
    if fetch is None:
        from src.portfolio.fmp import _get

        def fetch(sym, a, b):
            data = _get("historical-price-eod/full", sym, {"from": a, "to": b})
            return data if isinstance(data, list) else []
    cons = fetch_constituents(cons_path)
    today = date.today()
    members = sorted(members_asof(cons, today))
    a, b = (today - timedelta(days=days)).isoformat(), today.isoformat()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("create table if not exists pit_bars (symbol text, date text, close real, volume real, primary key(symbol, date))")
    db.execute("create table if not exists pit_fetched (symbol text primary key, rows int, fetched_at text)")
    n_rows, empty = 0, []
    for s in members:
        try:
            data = fetch(s, a, b)
        except Exception:
            data = []
        rows = [(s, r["date"], float(r["close"]), float(r.get("volume") or 0)) for r in data if r.get("close") is not None]
        if rows:
            db.executemany("insert or replace into pit_bars values (?,?,?,?)", rows)
            db.execute("insert or replace into pit_fetched values (?,?,datetime('now'))", (s, len(rows)))
            n_rows += len(rows)
        else:
            empty.append(s)
        db.commit()
        if sleep_s:
            time.sleep(sleep_s)
    db.close()
    return {"members": len(members), "rows": n_rows, "empty": empty, "latest": latest_bar_date(db_path)}


def current_screen(n: int = 50, asof: date | None = None, db_path: Path = PIT_DB,
                   cons_path: Path = CONSTITUENTS, **screen_kw) -> list[str]:
    """Today's (or `asof`) top-`n` by the same momentum × liquidity screen MarsWalk uses, in rank
    order. [] when the store is missing."""
    if not pit_available(db_path, cons_path):
        return []
    d = (asof or date.today()).isoformat()
    cons = load_constituents(cons_path)
    members = members_asof(cons, d)
    bars = load_bars(members, db_path)
    return screen_asof(bars, members, d, n=n, **screen_kw)
