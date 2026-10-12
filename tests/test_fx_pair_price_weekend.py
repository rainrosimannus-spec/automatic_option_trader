"""An FX pair still has a price when the FX market is shut.

IBKR's snapshot is empty all weekend and the account reports no rate for a currency it does not
hold. Tel Aviv trades on Sunday, so every Sunday the shekel had no rate at all and Elbit's one
fully-funded buying window of the week was refused ("no IBKR rate (cache or pair)", 2026-10-11).
The last daily MIDPOINT close stands in.
"""
import math
from types import SimpleNamespace

import src.portfolio.connection as conn
from src.portfolio import buyer


class _IB:
    def __init__(self, snapshot_mid=float("nan"), closes=(3.4331, 3.4291)):
        self.snapshot_mid, self.closes = snapshot_mid, closes
        self.hist_calls = 0

    def reqMktData(self, *a):
        m = self.snapshot_mid
        return SimpleNamespace(midpoint=lambda: m, marketPrice=lambda: m, last=m, close=m)

    def cancelMktData(self, *a):
        pass

    def sleep(self, *a):
        pass

    def reqHistoricalData(self, *a, **k):
        self.hist_calls += 1
        return [SimpleNamespace(date=f"2026-10-1{i}", close=c) for i, c in enumerate(self.closes)]


def _pair():
    return SimpleNamespace(symbol="EUR", currency="ILS")


def test_live_mid_is_used_when_the_market_is_open():
    ib = _IB(snapshot_mid=3.44)
    assert conn.fx_pair_price(ib, _pair()) == 3.44 and ib.hist_calls == 0


def test_last_daily_close_stands_in_on_a_sunday():
    ib = _IB()                                        # NaN snapshot: FX shut
    assert conn.fx_pair_price(ib, _pair()) == 3.4291 and ib.hist_calls == 1


def test_nothing_at_all_gives_none():
    ib = _IB(closes=())
    assert conn.fx_pair_price(ib, _pair()) is None


def test_buyer_sizes_a_shekel_buy_from_the_weekend_close(monkeypatch):
    ib = _IB()
    monkeypatch.setattr(buyer, "get_portfolio_lock", lambda: conn._portfolio_lock)
    assert buyer._fx_rate_ccy_per_base(ib, _pair(), "EUR", "ILS") == 3.4291        # ILS per EUR
    assert math.isclose(1 / buyer._fx_rate_ccy_per_base(ib, _pair(), "EUR", "ILS"), 0.2916, abs_tol=1e-4)
