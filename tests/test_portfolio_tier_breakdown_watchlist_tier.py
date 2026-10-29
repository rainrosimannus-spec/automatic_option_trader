"""/portfolio tier slices follow the WATCHLIST tier, not the holding row's frozen first-buy tier.

The monthly screen reclassifies names on the watchlist row only (2026-10-06: VRT/NU/6920/6146
breakthrough→growth, EUR 784k); the holding row keeps the tier it was bought under. The page must
show the tier the capital is managed under, with the holding tier only as fallback.
"""
from types import SimpleNamespace as NS

from src.web.routes.portfolio import _build_tier_breakdown


def _h(sym, tier, mv, ccy="EUR"):
    return NS(symbol=sym, tier=tier, market_value=mv, currency=ccy)


def test_watchlist_tier_overrides_holding_tier():
    holdings = [_h("VRT", "breakthrough", 100.0), _h("AAPL", "growth", 50.0), _h("T", "dividend", 10.0)]
    rates = {"EUR": 1.0}
    stale = _build_tier_breakdown(holdings, rates)
    assert stale["breakthrough"] == 100.0 and stale["growth"] == 50.0

    fresh = _build_tier_breakdown(holdings, rates, tier_of={"VRT": "growth"})
    assert fresh["breakthrough"] == 0 and fresh["growth"] == 150.0
    assert fresh["dividend"] == 10.0
    assert sum(fresh.values()) == sum(stale.values())        # money never moves, only the label


def test_name_off_the_watchlist_keeps_holding_tier():
    holdings = [_h("OLD", "breakthrough", 7.0)]
    out = _build_tier_breakdown(holdings, {"EUR": 1.0}, tier_of={"OTHER": "dividend"})
    assert out["breakthrough"] == 7.0


def test_tier_returns_follow_same_buckets_as_slices():
    from src.web.routes.portfolio import _build_tier_returns
    holdings = [NS(symbol="VRT", tier="breakthrough", market_value=120.0, currency="EUR", total_invested=100.0),
                NS(symbol="T", tier="dividend", market_value=90.0, currency="EUR", total_invested=100.0),
                NS(symbol="NEW", tier="growth", market_value=0.0, currency="EUR", total_invested=0.0)]
    r = _build_tier_returns(holdings, {"EUR": 1.0}, tier_of={"VRT": "growth"})
    assert abs(r["growth"] - 20.0) < 1e-9   # VRT counted as growth, like the slice
    assert r["breakthrough"] is None      # empty tier prints nothing
    assert abs(r["dividend"] + 10.0) < 1e-9
