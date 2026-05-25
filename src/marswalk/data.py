"""
Historical price + IV fetch/cache for MarsWalk.

The ONLY place MarsWalk touches IBKR: read-only historical data via
reqHistoricalData, on a DEDICATED clientId, on its own short-lived connection.
No orders, never touches trades.db. Heavy (competes for the gateway) — intended
to run OFF-HOURS. Cached bars are param-invariant, so they're fetched once per
regime and reused across every DTE/delta run.
"""
from __future__ import annotations

from datetime import datetime

from src.core.logger import get_logger
from src.marswalk.models import get_mw_db, MarketBar

log = get_logger("marswalk.data")


def _pdate(s: str):
    s = str(s)
    if "-" in s:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    return datetime.strptime(s[:8], "%Y%m%d").date()


def has_data(regime_id: str, symbol: str) -> bool:
    with get_mw_db() as db:
        return db.query(MarketBar).filter_by(
            regime_id=regime_id, symbol=symbol).first() is not None


def load_market(regime, universe):
    """{symbol: [(date, close, iv), ...]} from cache, symbols with >=5 bars."""
    out = {}
    with get_mw_db() as db:
        for sym in universe:
            rows = (db.query(MarketBar)
                    .filter_by(regime_id=regime.id, symbol=sym)
                    .order_by(MarketBar.date).all())
            bars = [(_pdate(r.date), r.close, r.iv) for r in rows if r.close and r.iv]
            if len(bars) >= 5:
                out[sym] = bars
    return out


def _store(regime_id, symbol, rows):
    if not rows:
        return
    with get_mw_db() as db:
        for ds, close, iv in rows:
            existing = db.query(MarketBar).filter_by(
                regime_id=regime_id, symbol=symbol, date=ds).first()
            if existing:
                existing.close, existing.iv = close, iv
            else:
                db.add(MarketBar(regime_id=regime_id, symbol=symbol,
                                 date=ds, close=close, iv=iv))


def fetch_and_cache(regime, universe, force: bool = False):
    """Fetch daily close (TRADES) + IV (OPTION_IMPLIED_VOLATILITY) per symbol for
    the regime window and cache to marswalk.db.

    REUSES THE LIVE PORTFOLIO CONNECTION under get_portfolio_lock() — exactly like
    refresh_brkb_history. A standalone IB() on a fresh asyncio loop fights the live
    connection and silently times out, so this only works IN-PROCESS (run via the
    weekly sweep job or Run-now, both inside the trader). _ensure_event_loop() binds
    this thread to the shared portfolio loop; the lock serializes loop access.
    """
    from ib_insync import Stock
    from src.portfolio.connection import (
        get_portfolio_ib, get_portfolio_lock, is_portfolio_connected, _ensure_event_loop,
    )

    _ensure_event_loop()
    if not is_portfolio_connected():
        log.warning("marswalk_portfolio_not_connected", regime=regime.id)
        return
    ib = get_portfolio_ib()

    start, end = _pdate(regime.start), _pdate(regime.end)
    duration = f"{(end - start).days + 10} D"
    # datetime object (not pre-formatted string) — ib_insync formats it per API version.
    end_dt = datetime.combine(end, datetime.min.time()).replace(hour=23, minute=59, second=59)

    for sym in universe:
        if not force and has_data(regime.id, sym):
            continue
        try:
            c = Stock(sym, "SMART", "USD")
            # TRADES (not ADJUSTED_LAST): IBKR rejects ADJUSTED_LAST with a past
            # endDateTime (Error 321). Unadjusted, so a split-crossing window shows
            # a discontinuity — known caveat (e.g. TSLA/AMZN in 2022, NVDA 2021/24).
            with get_portfolio_lock():
                ib.qualifyContracts(c)
                c.exchange = "SMART"
                price_bars = ib.reqHistoricalData(
                    c, endDateTime=end_dt, durationStr=duration,
                    barSizeSetting="1 day", whatToShow="TRADES",
                    useRTH=True, formatDate=1, timeout=40)
                iv_bars = ib.reqHistoricalData(
                    c, endDateTime=end_dt, durationStr=duration,
                    barSizeSetting="1 day", whatToShow="OPTION_IMPLIED_VOLATILITY",
                    useRTH=False, formatDate=1, timeout=40)
                ib.sleep(0.3)  # gentle pacing, while we hold the lock
            iv_by = {}
            for b in iv_bars:
                if b.close and b.close > 0:
                    iv_by[_pdate(b.date).strftime("%Y-%m-%d")] = float(b.close)
            rows = []
            for b in price_bars:
                d = _pdate(b.date)
                if d < start or d > end or not b.close:
                    continue
                ds = d.strftime("%Y-%m-%d")
                iv = iv_by.get(ds)
                if not iv:
                    continue
                rows.append((ds, float(b.close), iv))
            _store(regime.id, sym, rows)
            log.info("marswalk_data_cached", regime=regime.id, symbol=sym, bars=len(rows))
        except Exception as e:
            log.warning("marswalk_fetch_failed", regime=regime.id, symbol=sym, error=str(e))


def ensure_market_data(regime, universe):
    """Fetch only the symbols not already cached for this regime."""
    missing = [s for s in universe if not has_data(regime.id, s)]
    if missing:
        log.info("marswalk_fetching", regime=regime.id, symbols=len(missing))
        fetch_and_cache(regime, missing)
