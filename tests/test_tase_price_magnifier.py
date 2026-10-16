"""Tel Aviv prices: quotes AND orders are in agorot; priceMagnifier=100 is only the display scale.

2026-10-16, proven with what-if orders on ESLT (178 shares): a limit of 231,800 carries commission
₪412.60 — 0.1% of ₪412,604 — while a limit of 2,318 carries the ₪15 minimum (notional ₪4,126). So
the API prices TASE orders in the QUOTE unit, exactly like London pence. Commit 93e0829 had assumed
the opposite (divide by the magnifier) and sent 178 ESLT at 2,318 = ₪23.18 against a ₪2,302 market:
accepted, sat below the market for 55 minutes, expired unfilled at the Friday close.

What the magnifier DOES change is the tick table: market rule 1266 reads (0→0.001, 10→0.01,
100→0.1, 1000→1) in shekels, so the grid in agorot is 0.1 / 1 / 10 / 100 at 1,000 / 10,000 /
100,000 agorot (mapped: 999.5 ok; 9,999 ok; 50,010 ok, 50,001 refused; 100,010 refused; 231,900 ok,
231,850 / 231,810 / 231,801 refused). The Oct-14 refusal of 219,954 was that tick error, not a unit
error. London is the control: pence quote, priceMagnifier 1, orders in pence — unchanged.
"""
from types import SimpleNamespace

import pytest

from src.core import quote_units as qu
from src.portfolio import buyer as b
from src.portfolio.buyer import (_price_magnifier, _close_hour, _MARKET_HOURS, _EARLY_CLOSE,
                                 _venue_band_tick, _market_rule_tick, _effective_tick, _round_to_tick)

_RULE_1266 = [SimpleNamespace(lowEdge=0.0, increment=0.001), SimpleNamespace(lowEdge=10.0, increment=0.01),
              SimpleNamespace(lowEdge=100.0, increment=0.1), SimpleNamespace(lowEdge=1000.0, increment=1.0)]


def _details(magnifier, rule="1266", exchanges="TASE"):
    return [SimpleNamespace(priceMagnifier=magnifier, minTick=0.001, marketRuleIds=rule,
                            validExchanges=exchanges)]


class _IB:
    def reqMarketRule(self, rid):
        return _RULE_1266


@pytest.fixture(autouse=True)
def _fresh_rule_cache(monkeypatch):
    monkeypatch.setattr(b, "_MARKET_RULE_CACHE", {})
    monkeypatch.setattr(b, "_MIN_TICK_CACHE", {})


def test_magnifier_read_from_details():
    assert _price_magnifier(_details(100)) == 100.0
    assert _price_magnifier(_details(1)) == 1.0


def test_magnifier_defaults_to_one():
    assert _price_magnifier(None) == 1.0
    assert _price_magnifier([]) == 1.0
    assert _price_magnifier(_details(0)) == 1.0
    assert _price_magnifier([SimpleNamespace()]) == 1.0


# ── the order unit is the quote unit ──────────────────────────────────────────────────────

def test_tase_order_price_is_agorot_like_the_quote():
    """₪2,318.64 → 231,864 agorot is what goes on the wire (snapped to the 100-agorot grid)."""
    quote = qu.major_to_quote(2318.64, "ILS")
    assert quote == pytest.approx(231864.0)
    assert _round_to_tick(quote, 100.0, "BUY") == 231800.0
    assert qu.quote_to_major(231800.0, "ILS") == pytest.approx(2318.0)


def test_london_unchanged():
    assert qu.major_to_quote(135.75, "GBP") == pytest.approx(13575.0)
    assert _market_rule_tick(_IB(), _details(1, exchanges="LSE"), "LSE", 13575.0) == pytest.approx(1.0)


# ── the tick table is in the display unit and scales by the magnifier ─────────────────────

@pytest.mark.parametrize("agorot, tick", [
    (999.5, 0.1), (9_999.0, 1.0), (50_010.0, 10.0), (100_010.0, 100.0), (231_864.0, 100.0),
])
def test_market_rule_tick_scaled_into_agorot(agorot, tick):
    assert _market_rule_tick(_IB(), _details(100), "TASE", agorot) == pytest.approx(tick)


@pytest.mark.parametrize("agorot, tick", [
    (999.5, 0.1), (1_000.0, 1.0), (9_999.0, 1.0), (10_000.0, 10.0), (50_010.0, 10.0),
    (99_999.0, 10.0), (100_000.0, 100.0), (231_864.0, 100.0),
])
def test_ils_band_table_matches_the_venue(agorot, tick):
    """Safety net for callers without contract details (ladder rungs, re-pricing)."""
    assert _venue_band_tick("ILS", agorot) == pytest.approx(tick)


def test_effective_tick_for_eslt_is_100_agorot():
    assert _effective_tick(_IB(), SimpleNamespace(conId=1, currency="ILS", exchange="TASE"),
                           231864.0, details=_details(100), currency="ILS", exchange="TASE") == 100.0
    # the magnifier-scaled minTick (0.001 shekel = 0.1 agorot) never outranks the real grid
    assert _effective_tick(_IB(), SimpleNamespace(conId=2, currency="ILS", exchange="TASE"),
                           999.5, details=_details(100), currency="ILS", exchange="TASE") == pytest.approx(0.1)


def test_the_oct14_price_and_the_oct16_price_snap_onto_the_grid():
    """219,954 (refused Oct-14) → 219,900; 231,864 → 231,800; a SELL rounds up."""
    tick = _market_rule_tick(_IB(), _details(100), "TASE", 219954.0)
    assert tick == 100.0
    assert _round_to_tick(219954.0, tick, "BUY") == 219900.0
    assert _round_to_tick(231864.0, tick, "SELL") == 231900.0


def test_band_tick_other_venues_untouched():
    assert _venue_band_tick("USD", 231.5) == 0.0
    assert _venue_band_tick("GBP", 13575.0) == 0.0
    assert _venue_band_tick("JPY", 9540.0) > 0.0


# ── fills: agorot expected, the watchlist price arbitrates until the first one is seen ────

def test_fill_in_agorot_is_divided():
    assert qu.fill_price_to_major(231800.0, "ILS", reference_major=2318.0) == pytest.approx(2318.0)


def test_fill_already_in_shekels_is_kept():
    assert qu.fill_price_to_major(2318.0, "ILS", reference_major=2300.0) == pytest.approx(2318.0)


def test_fill_without_reference_uses_plain_division():
    assert qu.fill_price_to_major(220000.0, "ILS") == pytest.approx(2200.0)
    assert qu.fill_price_to_major(2200.0, "ILS", reference_major=0) == pytest.approx(22.0)


def test_fill_major_unit_currency_untouched():
    assert qu.fill_price_to_major(5780.0, "JPY", reference_major=57.8) == 5780.0
    assert qu.fill_price_to_major(253.1, "USD", reference_major=2531.0) == 253.1


def test_london_fill_in_pence_still_divided():
    assert qu.fill_price_to_major(13575.0, "GBP", reference_major=135.0) == pytest.approx(135.75)


# ── the short Friday ──────────────────────────────────────────────────────────────────────

def test_tase_friday_close_hour():
    _tz, _open, close_h, days = _MARKET_HOURS["ILS"]
    assert days == (0, 1, 2, 3, 4)
    assert _close_hour("ILS", close_h, 4) == 13
    assert _close_hour("ILS", close_h, 3) == close_h


def test_early_close_only_where_listed():
    for ccy, (_tz, _open, close_h, _days) in _MARKET_HOURS.items():
        if ccy in _EARLY_CLOSE:
            continue
        for wd in range(7):
            assert _close_hour(ccy, close_h, wd) == close_h
