"""IBKR positions, not card status, are what stop a gap being bought twice (NU 2026-09-30).

NU was bought twice for one gap — USD 223,650 into a ~112k hole, two days' DCA budget in 17 minutes.
Both scans logged deployed_today=5459, identical.

    19:50:12  order placed, Submitted
    19:58:13  FULLY FILLED
    20:05:46  second scan buys it again
    20:17:05  trade_sync finally lands all eight executions

Four successive guards have tried to infer "already bought" from TradeSuggestion.status, each adding
the one status the latest incident exposed. It cannot work: _expire_orphan_buy_suggestions runs
earlier in the SAME scan and rewrites a placed-and-filled card to 'expired', because it cannot tell
"the order died" from "the order filled" — both look like a submitted card with no working order.
The log proves it fired at 2026-09-30 20:05:46, count=1, the exact second of the second scan.

So this compares IBKR's live position against the holdings snapshot instead. No status, no timestamp
(holdings.updated_at is onupdate=utcnow, so the hourly price job bumps it without touching shares).
"""
import datetime as dt
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base
from src.portfolio.models import PortfolioHolding
from src.portfolio.buyer import PortfolioBuyer
import src.portfolio.buyer as buyer_mod
import src.portfolio.fx as pfx


class _Contract:
    def __init__(self, symbol, currency="USD", secType="STK"):
        self.symbol, self.currency, self.secType = symbol, currency, secType


class _Pos:
    def __init__(self, symbol, position, avgCost=0.0, currency="USD", secType="STK"):
        self.contract = _Contract(symbol, currency, secType)
        self.position, self.avgCost = position, avgCost


def _setup(monkeypatch, positions, holdings, rates=None):
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()
    for sym, sh, px in holdings:
        sess.add(PortfolioHolding(symbol=sym, shares=sh, current_price=px,
                                  updated_at=dt.datetime(2026, 9, 30, 12, 0)))
    sess.flush()

    @contextmanager
    def fake_get_db():
        yield sess

    @contextmanager
    def fake_lock():
        yield

    monkeypatch.setattr(buyer_mod, "get_db", fake_get_db)
    monkeypatch.setattr(buyer_mod, "get_portfolio_lock", fake_lock)
    monkeypatch.setattr(pfx, "load_fx_rates", lambda: rates or {})
    monkeypatch.setattr(pfx, "to_base",
                        lambda n, ccy, r: n * (r or {}).get(ccy, 1.0))

    b = PortfolioBuyer.__new__(PortfolioBuyer)
    class _Cfg:
        cash_yield_symbol = "XEON"
    b.cfg = _Cfg()
    class _IB:
        def positions(self_inner):
            return positions
    b.ib = _IB()
    return b


# ── the incident ──────────────────────────────────────────────────────────────

def test_replays_the_nu_double_buy(monkeypatch):
    """At 20:05:46 IBKR held 14,361 NU; the holdings snapshot still said 5,521. The difference is
    the first buy, and folding it is what stops the second."""
    b = _setup(monkeypatch,
               positions=[_Pos("NU", 14_361, avgCost=13.0)],
               holdings=[("NU", 5_521, 12.68)])
    got = b._ibkr_position_shortfall_map()
    assert round(got["NU"]) == round(8_840 * 12.68)      # ≈ 112,091


def test_db_level_or_ahead_contributes_nothing(monkeypatch):
    """Can only ever ADD. A stale-high holdings row must never be able to unblock buying."""
    b = _setup(monkeypatch, positions=[_Pos("NU", 5_521, avgCost=13.0)],
               holdings=[("NU", 5_521, 12.68)])
    assert b._ibkr_position_shortfall_map() == {}
    b = _setup(monkeypatch, positions=[_Pos("NU", 4_000, avgCost=13.0)],
               holdings=[("NU", 5_521, 12.68)])
    assert b._ibkr_position_shortfall_map() == {}


def test_self_zeroes_once_the_sync_lands(monkeypatch):
    """No timestamp involved — the moment holdings carries the shares, the fold disappears."""
    b = _setup(monkeypatch, positions=[_Pos("NU", 14_361, avgCost=13.0)],
               holdings=[("NU", 14_361, 12.68)])
    assert "NU" not in b._ibkr_position_shortfall_map()


def test_brand_new_position_with_no_holdings_row(monkeypatch):
    b = _setup(monkeypatch, positions=[_Pos("S", 1_000, avgCost=25.0)], holdings=[])
    assert round(b._ibkr_position_shortfall_map()["S"]) == 25_000   # priced off avgCost


# ── exclusions and typing ─────────────────────────────────────────────────────

def test_park_etf_excluded(monkeypatch):
    """Parking idle cash in XEON is not compounder deployment — same exclusion as everywhere else."""
    b = _setup(monkeypatch, positions=[_Pos("XEON", 500_000, avgCost=100.0)], holdings=[])
    assert b._ibkr_position_shortfall_map() == {}


def test_non_stock_positions_ignored(monkeypatch):
    b = _setup(monkeypatch,
               positions=[_Pos("NU", 100, avgCost=13.0, secType="OPT"),
                          _Pos("EUR", 1, avgCost=1.0, secType="CASH")],
               holdings=[])
    assert b._ibkr_position_shortfall_map() == {}


def test_foreign_position_is_fx_normalised(monkeypatch):
    """A JPY shortfall must reach the budget in base currency, not as raw yen."""
    b = _setup(monkeypatch,
               positions=[_Pos("6920", 300, avgCost=40_000.0, currency="JPY")],
               holdings=[("6920", 200, 39_000.0)],
               rates={"JPY": 0.0054311})
    assert round(b._ibkr_position_shortfall_map()["6920"]) == round(100 * 39_000 * 0.0054311)


def test_zero_price_is_skipped_not_counted_as_free(monkeypatch):
    b = _setup(monkeypatch, positions=[_Pos("NU", 1_000, avgCost=0.0)],
               holdings=[("NU", 0, 0.0)])
    assert b._ibkr_position_shortfall_map() == {}


# ── failure behaviour: never silently weaker than the old guard ───────────────

def test_returns_none_when_ibkr_has_nothing_cached(monkeypatch):
    """None, not {} — the caller must fall back to the status map rather than treat it as 'no gap'."""
    b = _setup(monkeypatch, positions=[], holdings=[("NU", 5_521, 12.68)])
    assert b._ibkr_position_shortfall_map() is None


def test_returns_none_when_the_positions_call_raises(monkeypatch):
    b = _setup(monkeypatch, positions=[], holdings=[])
    class _Boom:
        def positions(self):
            raise RuntimeError("not connected")
    b.ib = _Boom()
    assert b._ibkr_position_shortfall_map() is None


def test_caller_prefers_ibkr_and_never_folds_both(monkeypatch):
    """Either/or: folding the IBKR shortfall AND the status map would double-count one fill."""
    import inspect
    src = inspect.getsource(PortfolioBuyer.run_compounder_scan)
    assert "_ibkr_position_shortfall_map()" in src
    assert "if _shortfall is None:" in src
    assert src.count("_unsynced_executed_buy_map()") == 1   # fallback only
