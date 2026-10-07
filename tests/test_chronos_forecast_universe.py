"""
Chronos nightly forecast: the job crashed every night 2026-04-05 → 2026-10-07 on a watchlist
column that never existed. These pin the replacement universe rule, the age-tolerant lookup
its consumers now use, and the guard flag that keeps the buy gate OFF until switched on.
"""
from datetime import date, timedelta
from types import SimpleNamespace as NS

from src.portfolio.forecaster import forecast_universe, latest_forecast
from src.portfolio.config import PortfolioConfig


def _row(symbol, currency, pending_removal=False):
    return NS(symbol=symbol, currency=currency, exchange="SMART", pending_removal=pending_removal)


def test_universe_keeps_tradable_and_held_drops_park_and_blocked_venues():
    rows = [
        _row("NVDA", "USD"),
        _row("XEON", "EUR"),                 # parked cash-yield ETF → out
        _row("RELIANCE", "INR"),             # untradable venue → out
        _row("FSR", "ZAR"),                  # untradable venue → out
        _row("LRCX", "USD", pending_removal=True),   # held, dropped from screen → STAYS (exit review needs its trend)
        _row("7203", "JPY"),
    ]
    out = [r.symbol for r in forecast_universe(rows, park_symbol="XEON")]
    assert out == ["NVDA", "LRCX", "7203"]


def test_universe_without_park_symbol_keeps_everything_tradable():
    rows = [_row("XEON", "EUR"), _row("NVDA", "USD")]
    assert [r.symbol for r in forecast_universe(rows, park_symbol=None)] == ["XEON", "NVDA"]


def test_guard_flag_ships_off():
    assert PortfolioConfig().chronos_guard_enabled is False


def test_latest_forecast_prefers_newest_within_window(monkeypatch):
    """Rows: 1 day old and 5 days old → returns the 1-day-old one; a 5-day-old row alone is
    too stale for the 3-day window and returns None."""
    import src.portfolio.forecaster as fc

    class _Q:
        def __init__(self, rows): self.rows = rows
        def filter(self, *clauses):
            # emulate the two clauses: symbol match + forecast_date >= cutoff
            cutoff = None
            for c in clauses:
                right = getattr(c, "right", None)
                v = getattr(right, "value", None)
                if isinstance(v, str) and len(v) == 10 and v[4] == "-":
                    cutoff = v
            self.rows = [r for r in self.rows if cutoff is None or r.forecast_date >= cutoff]
            return self
        def order_by(self, *a): self.rows = sorted(self.rows, key=lambda r: r.forecast_date, reverse=True); return self
        def first(self): return self.rows[0] if self.rows else None

    class _DB:
        def __init__(self, rows): self.rows = rows
        def query(self, model): return _Q(list(self.rows))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    today = date.today()
    d = lambda n: (today - timedelta(days=n)).strftime("%Y-%m-%d")
    fresh = NS(symbol="NVDA", forecast_date=d(1), trend="down")
    stale = NS(symbol="NVDA", forecast_date=d(5), trend="up")

    monkeypatch.setattr(fc, "get_db", lambda: _DB([stale, fresh]))
    assert latest_forecast("NVDA").forecast_date == d(1)

    monkeypatch.setattr(fc, "get_db", lambda: _DB([stale]))
    assert latest_forecast("NVDA") is None
