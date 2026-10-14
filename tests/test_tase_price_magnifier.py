"""Tel Aviv prices: IBKR QUOTES TASE in agorot but ORDERS are in shekels (priceMagnifier=100).

2026-10-14: the compounder's first ILS buy (ESLT, 2,199.54 shekels) was sent at 219,954 — the agorot
quote — and sat PendingSubmit until the stuck-order guard cancelled it and paused the symbol for 6 h.
A what-if at 219,954 is refused ("does not conform to the minimum price variation"), at 2,200 it is
accepted. London is the control: pence quote, priceMagnifier 1, orders in pence — unchanged.
"""
from types import SimpleNamespace

import pytest

from src.core import quote_units as qu
from src.portfolio.buyer import _price_magnifier, _close_hour, _MARKET_HOURS, _EARLY_CLOSE


def _details(magnifier):
    return [SimpleNamespace(priceMagnifier=magnifier, minTick=0.001)]


def test_magnifier_read_from_details():
    assert _price_magnifier(_details(100)) == 100.0
    assert _price_magnifier(_details(1)) == 1.0


def test_magnifier_defaults_to_one():
    assert _price_magnifier(None) == 1.0
    assert _price_magnifier([]) == 1.0
    assert _price_magnifier(_details(0)) == 1.0
    assert _price_magnifier([SimpleNamespace()]) == 1.0


def test_tase_order_price_is_shekels():
    """2,199.54 shekels → 219,954 agorot (quote unit) → 2,199.54 in the order unit; London stays pence."""
    quote = qu.major_to_quote(2199.54, "ILS")
    assert quote == pytest.approx(219954.0)
    assert quote / _price_magnifier(_details(100)) == pytest.approx(2199.54)
    pence = qu.major_to_quote(135.75, "GBP")
    assert pence / _price_magnifier(_details(1)) == pytest.approx(13575.0)


def test_settlement_notional_goes_back_through_the_magnifier():
    """Funding is in MAJOR units: order price (shekels) × magnifier → agorot → /100 → shekels again."""
    order_price = 2200.0
    assert qu.quote_to_major(order_price * 100, "ILS") == pytest.approx(2200.0)


# ── fills: shekels or agorot, the watchlist price decides ─────────────────────────────────

def test_fill_in_agorot_is_divided():
    assert qu.fill_price_to_major(220000.0, "ILS", reference_major=2150.0) == pytest.approx(2200.0)


def test_fill_already_in_shekels_is_kept():
    assert qu.fill_price_to_major(2200.0, "ILS", reference_major=2150.0) == pytest.approx(2200.0)


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
