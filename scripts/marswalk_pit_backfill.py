#!/usr/bin/env python3
"""Build / refresh the MarsWalk point-in-time universe store (2026-11-24 fidelity audit).

  .venv/bin/python scripts/marswalk_pit_backfill.py            # constituents + prices for new names
  .venv/bin/python scripts/marswalk_pit_backfill.py --force    # refetch every name

Writes data/marswalk_pit/constituents.json (FMP S&P 500 / Nasdaq-100 current lists + change logs)
and data/marswalk_pit.db (pit_bars: symbol, date, close, volume since 2016-01 for every name that was
a member at any point since 2017-01). ~12 minutes for 676 names on a fresh store. Read-only on the
trading side; src/marswalk/pit_universe.py consumes the store. FMP's history lacks a few delisted
tickers (DWDP, RHT): names that vanished are under-represented — stated on every PIT result.
"""
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.marswalk import pit_universe as pit  # noqa: E402
from src.portfolio.fmp import _get  # noqa: E402

START = "2016-01-01"


def main(force: bool = False) -> None:
    cons = pit.fetch_constituents()
    print(f"constituents: S&P {len(cons['sp500']['current'])} / NDX {len(cons['ndx']['current'])}, "
          f"{len(cons['sp500']['changes']) + len(cons['ndx']['changes'])} change rows")
    names: set[str] = set()
    d = date(2017, 1, 1)
    while d <= date.today():
        names |= pit.members_asof(cons, d)
        d += timedelta(days=30)
    db = sqlite3.connect(pit.PIT_DB)
    db.execute("create table if not exists pit_bars (symbol text, date text, close real, volume real, primary key(symbol, date))")
    db.execute("create table if not exists pit_fetched (symbol text primary key, rows int, fetched_at text)")
    if force:
        db.execute("delete from pit_fetched")
    db.commit()
    done = {r[0] for r in db.execute("select symbol from pit_fetched")}
    todo = sorted(n for n in names if n not in done)
    print(f"{len(names)} names ever a member since 2019-06; fetching {len(todo)}")
    t0 = time.time(); empty = []
    for i, s in enumerate(todo, 1):
        data = _get("historical-price-eod/full", s, {"from": START, "to": date.today().isoformat()})
        rows = [(s, r["date"], float(r["close"]), float(r.get("volume") or 0)) for r in data
                if r.get("close") is not None] if isinstance(data, list) else []
        if rows:
            db.executemany("insert or replace into pit_bars values (?,?,?,?)", rows)
        else:
            empty.append(s)
        db.execute("insert or replace into pit_fetched values (?,?,datetime('now'))", (s, len(rows)))
        db.commit()
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
        time.sleep(0.05)
    n_rows, n_sym = db.execute("select count(*), count(distinct symbol) from pit_bars").fetchone()
    print(f"done: {n_sym} symbols, {n_rows:,} bars; no FMP history for {empty}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
