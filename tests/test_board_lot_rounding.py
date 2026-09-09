"""Board-lot (trading-unit) rounding for foreign portfolio orders (2026-07-30).

Tokyo (and other lot-based venues) reject any order size that isn't a multiple of the
stock's trading unit — 6920 got cancelled with Error 461 "should be a multiple of 100"
after a raw dollar/price share count of 454. _board_lot picks the unit (IBKR's reported
sizeIncrement/minSize, else the venue's known unit) so the caller can floor to it.
"""
from types import SimpleNamespace

from src.portfolio.buyer import _board_lot, _sub_lot_verdict


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


# ── Below one lot: round up or retire, never retry forever (2026-09-09) ─────────────────────
# 6920: gap 66 sh vs a 100-sh Tokyo lot floored to 0 and the 'approved' card was retried by the
# 30s executor 938 times in a day while the name sat 10% underweight.

def test_gap_of_at_least_half_a_lot_rounds_up_to_one_lot():
    assert _sub_lot_verdict(66, 100, lot_value=24_500, eff_max=218_000) == "round_up"
    assert _sub_lot_verdict(50, 100, None, None) == "round_up"      # exactly half


def test_gap_under_half_a_lot_is_skipped():
    assert _sub_lot_verdict(49, 100, lot_value=24_500, eff_max=218_000) == "skip"
    assert _sub_lot_verdict(1, 100, None, None) == "skip"


def test_one_lot_above_the_per_order_cap_is_skipped():
    # Half-lot rule passes, but one lot would exceed the NLV-scaled max single buy.
    assert _sub_lot_verdict(90, 100, lot_value=300_000, eff_max=218_000) == "skip"


def test_unknown_bounds_fall_back_to_the_half_lot_rule_only():
    assert _sub_lot_verdict(66, 100, lot_value=None, eff_max=218_000) == "round_up"
    assert _sub_lot_verdict(66, 100, lot_value=24_500, eff_max=None) == "round_up"
