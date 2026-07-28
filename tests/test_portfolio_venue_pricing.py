"""Foreign-venue pricing + currency guards for the PORTFOLIO (compounder) side.

Regression cover for 2026-07-28. Every foreign compounder order ever placed hung in PendingSubmit
and was then cancelled by the stuck-order guard, which stamped "venue rights" on it — a hardcoded
string, never a diagnosis. The account had the rights all along (a manual ASML buy filled fine).
The real causes were numeric:

  1. Limit prices were `round(price, 2)`, legal only where minTick == 0.01. Tokyo has no fractional
     yen, NSE ticks ₹0.05, Euronext ticks €0.50 on a €1.4k name — so IBKR never accepted the order.
     The control: INGA filled on the SAME venue in the SAME currency at €28.14, because at €28 the
     Euronext tick really is €0.01.
  2. `rate_to_base` fell back to 1.0 for any currency IBKR had not reported an ExchangeRate for,
     so an ITC order sized ₹287.28/share as if it were €287.28.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.portfolio import fx as pfx
from src.portfolio.buyer import _round_to_tick


# ── 1. Tick-grid snapping ────────────────────────────────────────────────────

def test_buy_rounds_down_sell_rounds_up():
    """Never cross the price you intended by rounding."""
    assert _round_to_tick(100.07, 0.05, "BUY") == 100.05
    assert _round_to_tick(100.07, 0.05, "SELL") == 100.10


def test_unknown_tick_falls_back_to_legacy_2dp():
    """A contract-details hiccup must degrade to today's behaviour, never block the order."""
    assert _round_to_tick(1425.2934, None, "BUY") == 1425.29
    assert _round_to_tick(1425.2934, 0, "BUY") == 1425.29


def test_price_already_on_grid_is_unchanged():
    assert _round_to_tick(1425.50, 0.50, "BUY") == 1425.50
    assert _round_to_tick(199.53, 0.01, "BUY") == 199.53


def test_real_orders_that_hung_become_legal_prices():
    """The exact limits IBKR refused to acknowledge, snapped onto each venue's real grid."""
    # ASML on AEB — Euronext ticks €0.50 at this price level.
    assert _round_to_tick(1425.29, 0.50, "BUY") == 1425.00
    # 6146 / 6920 / 4385 on TSEJ — Tokyo has no sub-yen tick.
    assert _round_to_tick(55697.10, 10.0, "BUY") == 55690.0
    assert _round_to_tick(38330.70, 10.0, "BUY") == 38330.0
    assert _round_to_tick(3858.77, 1.0, "BUY") == 3858.0
    # ITC on NSE — ₹0.05 grid.
    assert _round_to_tick(287.28, 0.05, "BUY") == 287.25


def test_snapped_buy_never_exceeds_the_requested_limit():
    """Rounding must not spend more than the ladder intended, on any grid."""
    for tick in (0.01, 0.05, 0.5, 1.0, 10.0, 100.0):
        for raw in (287.28, 1425.29, 38330.70, 55697.10):
            assert _round_to_tick(raw, tick, "BUY") <= raw + 1e-9


def test_us_prices_are_untouched_by_the_change():
    """Every USD fill in the book was already legal — the fix must not move them."""
    for px in (199.53, 302.48, 137.32, 14.18, 1311.00, 88.15):
        assert _round_to_tick(px, 0.01, "BUY") == px


def test_no_float_dust_in_snapped_price():
    """A tick of 0.1 must not yield 55697.100000000006 — IBKR rejects malformed prices."""
    out = _round_to_tick(55697.19, 0.1, "BUY")
    assert out == 55697.1
    assert len(str(out).split(".")[-1]) <= 6


# ── 2. Currency guard ────────────────────────────────────────────────────────

_RATES = {"EUR": 1.0, "USD": 0.8797164, "GBP": 1.169485, "JPY": 0.0053716, "CAD": 0.6232203}


def test_has_rate_true_for_base_and_cached():
    assert pfx.has_rate("EUR", _RATES) is True
    assert pfx.has_rate("JPY", _RATES) is True      # Tokyo sizing was always correct
    assert pfx.has_rate(None, _RATES) is True


def test_has_rate_false_for_uncached_currencies():
    """IBKR only reports an ExchangeRate for currencies the account already holds, so a
    first-ever buy in these prices at 1.0 unless the caller refuses."""
    for ccy in ("INR", "HKD", "AUD", "CHF", "SGD", "ZAR", "ILS"):
        assert pfx.has_rate(ccy, _RATES) is False


def test_rate_to_base_still_passes_through_when_unknown():
    """The 1.0 fallback stays (callers that only pass base amounts rely on it) — has_rate is the gate."""
    assert pfx.rate_to_base("INR", _RATES) == 1.0
    assert pfx.rate_to_base("JPY", _RATES) == 0.0053716


def test_the_itc_mis_sizing_is_now_refused():
    """₹287.28/share sized at rate 1.0 gave 196 shares (~€600) booked as €56,307 of budget."""
    brick, price_local = 56369.0, 287.28
    assert int(brick / (price_local * pfx.rate_to_base("INR", _RATES))) == 196   # the bug
    assert not pfx.has_rate("INR", _RATES)                                      # now skipped
