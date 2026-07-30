"""Board-lot (trading-unit) rounding for foreign portfolio orders (2026-07-30).

Tokyo (and other lot-based venues) reject any order size that isn't a multiple of the
stock's trading unit — 6920 got cancelled with Error 461 "should be a multiple of 100"
after a raw dollar/price share count of 454. _board_lot picks the unit (IBKR's reported
sizeIncrement/minSize, else the venue's known unit) so the caller can floor to it.
"""
from types import SimpleNamespace

from src.portfolio.buyer import _board_lot


def _details(**kw):
    return [SimpleNamespace(**kw)]


def test_jpy_fallback_is_100():
    # No size info from IBKR → Tokyo's 100-share unit via the currency fallback.
    assert _board_lot(None, "JPY") == 100


def test_us_default_is_one():
    assert _board_lot(None, "USD") == 1
    assert _board_lot(None, "EUR") == 1


def test_ibkr_size_increment_wins():
    assert _board_lot(_details(sizeIncrement=100, minSize=1), "JPY") == 100


def test_min_size_used_when_no_increment():
    assert _board_lot(_details(sizeIncrement=0, minSize=100), "HKD") == 100


def test_increment_of_one_falls_through_to_default():
    # A venue reporting a 1-share increment must not force rounding.
    assert _board_lot(_details(sizeIncrement=1, minSize=1), "EUR") == 1


def test_rounding_math_454_to_400():
    lot = _board_lot(None, "JPY")
    assert (454 // lot) * lot == 400
