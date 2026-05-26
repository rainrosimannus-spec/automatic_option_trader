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
        # VIX series (close only; iv stored 0) for the engine's halt gate.
        vrows = (db.query(MarketBar)
                 .filter_by(regime_id=regime.id, symbol="^VIX")
                 .order_by(MarketBar.date).all())
        vbars = [(_pdate(r.date), r.close, 0.0) for r in vrows if r.close]
        if vbars:
            out["^VIX"] = vbars
        # SPY series (close only) for the engine's MA50 clamp + trend filter.
        srows = (db.query(MarketBar)
                 .filter_by(regime_id=regime.id, symbol="^SPY")
                 .order_by(MarketBar.date).all())
        sbars = [(_pdate(r.date), r.close, 0.0) for r in srows if r.close]
        if sbars:
            out["^SPY"] = sbars
    return out


def fetch_spy_yahoo(regime, force: bool = False):
    """One-shot offline SPY backfill (uses Yahoo v8 chart endpoint, no API key).

    Used to seed ^SPY for backtest regimes when the live IBKR fetch path isn't
    available (e.g. backtesting from a fresh checkout). Fetches a window
    starting 80 calendar days before the regime so the 50d SMA is warm on day 1.
    Idempotent: skips if SPY already cached for the regime (unless force).
    """
    import urllib.request, json
    from datetime import timedelta
    if not force and has_data(regime.id, "^SPY"):
        return
    start = _pdate(regime.start) - timedelta(days=80)
    end = _pdate(regime.end) + timedelta(days=1)
    p1 = int(datetime.combine(start, datetime.min.time()).timestamp())
    p2 = int(datetime.combine(end, datetime.min.time()).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/SPY"
           f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        body = urllib.request.urlopen(req, timeout=20).read()
        payload = json.loads(body)
        result = payload["chart"]["result"][0]
        ts = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        rows = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            d = datetime.utcfromtimestamp(t).date()
            rows.append((d.strftime("%Y-%m-%d"), float(c), 0.0))
        if rows:
            _store(regime.id, "^SPY", rows)
            log.info("marswalk_spy_cached", regime=regime.id, bars=len(rows))
    except Exception as e:
        log.warning("marswalk_spy_yahoo_failed", regime=regime.id, error=str(e))


def fetch_symbols_yahoo(regime, symbols: list[str], force: bool = False):
    """Offline backfill of any equity universe via Yahoo v8 chart endpoint.

    Stores daily close + a realized-vol IV proxy (20-day trailing std of
    log-returns × sqrt(252)) so the engine's pricing model has reasonable
    vol input when IBKR data isn't available. Idempotent per (regime, symbol).
    """
    import urllib.request, json, math
    from datetime import timedelta
    start = _pdate(regime.start) - timedelta(days=80)  # 80d warm-up for realized-vol
    end = _pdate(regime.end) + timedelta(days=1)
    p1 = int(datetime.combine(start, datetime.min.time()).timestamp())
    p2 = int(datetime.combine(end, datetime.min.time()).timestamp())

    for sym in symbols:
        if not force and has_data(regime.id, sym):
            continue
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?period1={p1}&period2={p2}&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            body = urllib.request.urlopen(req, timeout=20).read()
            payload = json.loads(body)
            result = payload.get("chart", {}).get("result")
            if not result:
                log.warning("marswalk_yahoo_no_result", regime=regime.id, symbol=sym)
                continue
            result = result[0]
            ts = result.get("timestamp") or []
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
            # Build a date-ordered close list, dropping None bars.
            day_closes = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                d = datetime.utcfromtimestamp(t).date()
                day_closes.append((d, float(c)))
            if len(day_closes) < 22:
                continue
            # Trailing 20-day realized vol -> IV proxy (decimal, annualized).
            iv_window = 20
            rows = []
            regime_start = _pdate(regime.start)
            regime_end = _pdate(regime.end)
            for i in range(iv_window, len(day_closes)):
                d, c = day_closes[i]
                if d < regime_start or d > regime_end:
                    continue
                rets = []
                for j in range(i - iv_window, i):
                    prev_c = day_closes[j][1]
                    cur_c = day_closes[j+1][1] if j+1 < len(day_closes) else c
                    if prev_c > 0:
                        rets.append(math.log(cur_c / prev_c))
                if len(rets) < 5:
                    continue
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
                iv = math.sqrt(var) * math.sqrt(252)
                if iv <= 0:
                    continue
                rows.append((d.strftime("%Y-%m-%d"), c, iv))
            if rows:
                _store(regime.id, sym, rows)
        except Exception as e:
            log.warning("marswalk_yahoo_failed", regime=regime.id, symbol=sym, error=str(e))


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
    # IBKR rejects durationStr > 365 D for daily bars (this is why the full-year
    # 2021 regime fetched nothing). Use years for long windows.
    _dur_days = (end - start).days + 10
    duration = f"{_dur_days // 365 + 1} Y" if _dur_days > 365 else f"{_dur_days} D"
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

    # VIX index for the regime (engine halt gate). Stored close-only (iv=0).
    if force or not has_data(regime.id, "^VIX"):
        try:
            from ib_insync import Index
            vix = Index("VIX", "CBOE")
            with get_portfolio_lock():
                ib.qualifyContracts(vix)
                vbars = ib.reqHistoricalData(
                    vix, endDateTime=end_dt, durationStr=duration,
                    barSizeSetting="1 day", whatToShow="TRADES",
                    useRTH=True, formatDate=1, timeout=40)
                ib.sleep(0.3)
            vrows = [(_pdate(b.date).strftime("%Y-%m-%d"), float(b.close), 0.0)
                     for b in vbars if b.close and start <= _pdate(b.date) <= end]
            _store(regime.id, "^VIX", vrows)
            log.info("marswalk_vix_cached", regime=regime.id, bars=len(vrows))
        except Exception as e:
            log.warning("marswalk_vix_fetch_failed", regime=regime.id, error=str(e))


def fetch_earnings(universe, force: bool = False):
    """Cache historical earnings dates per symbol (FMP, regime-agnostic — fetched
    once and reused across all regimes). HTTP only, no IBKR. Stored under the
    sentinel regime_id '_earnings' (close/iv unused)."""
    try:
        from tools.screen_universe import _fmp_get
    except Exception as e:
        log.warning("marswalk_earnings_no_fmp", error=str(e))
        return
    for sym in universe:
        if not force and has_data("_earnings", sym):
            continue
        try:
            data = _fmp_get("earnings", sym, {"limit": 60}) or []
            rows = []
            for e in data:
                ds = str(e.get("date") or "")[:10]
                if len(ds) == 10:
                    rows.append((ds, 0.0, 0.0))
            _store("_earnings", sym, rows)
            log.info("marswalk_earnings_cached", symbol=sym, dates=len(rows))
        except Exception as ex:
            log.warning("marswalk_earnings_fetch_failed", symbol=sym, error=str(ex))


def load_earnings(universe):
    """{symbol: set(earnings_date)} from cache for the earnings gate."""
    out = {}
    with get_mw_db() as db:
        for sym in universe:
            rows = db.query(MarketBar).filter_by(regime_id="_earnings", symbol=sym).all()
            dates = {_pdate(r.date) for r in rows}
            if dates:
                out[sym] = dates
    return out


def ensure_market_data(regime, universe):
    """Fetch symbols not cached for this regime (+ the VIX series + earnings dates)."""
    missing = [s for s in universe if not has_data(regime.id, s)]
    if missing or not has_data(regime.id, "^VIX"):
        log.info("marswalk_fetching", regime=regime.id, symbols=len(missing))
        fetch_and_cache(regime, missing)  # also fetches ^VIX if absent
    if any(not has_data("_earnings", s) for s in universe):
        fetch_earnings(universe)  # global earnings dates (once)
