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


# ── 3. IBKR's minTick is a FLOOR, not the applicable tick ────────────────────
# 2026-07-29: the first fix trusted ContractDetails.minTick. On Tokyo IBKR returns 0.1 -- the
# FINEST tick across every price band -- so the snap was a no-op and the orders hung again:
#     6920  raw 35,617.2  -> "ticked" 35,617.2   (TSE has no fractional yen at ANY price)
#     6146  raw 52,471.05 -> "ticked" 52,471.0   (real grid there is Y100)
# Fix: round on max(IBKR minTick, the venue's band tick at this price).

from src.portfolio.buyer import _venue_band_tick


def _snap_jpy(raw: float, ibkr_tick: float = 0.1) -> float:
    return _round_to_tick(raw, max(ibkr_tick, _venue_band_tick("JPY", raw)), "BUY")


def test_tse_band_tick_widens_with_price():
    assert _venue_band_tick("JPY", 2_500) == 1
    assert _venue_band_tick("JPY", 4_000) == 5
    assert _venue_band_tick("JPY", 20_000) == 10
    assert _venue_band_tick("JPY", 40_000) == 50
    assert _venue_band_tick("JPY", 100_000) == 100
    assert _venue_band_tick("JPY", 60_000_000) == 100_000


def test_every_japanese_order_that_hung_now_snaps_to_whole_yen():
    """The five real limits IBKR refused to acknowledge."""
    assert _snap_jpy(35_617.20) == 35_600      # 6920, 2026-07-29
    assert _snap_jpy(52_471.05) == 52_400      # 6146, 2026-07-29
    assert _snap_jpy(38_330.70) == 38_300      # 6920, 2026-07-28
    assert _snap_jpy(55_697.10) == 55_600      # 6146, 2026-07-28
    assert _snap_jpy(3_858.77) == 3_855        # 4385


def test_no_japanese_price_can_carry_a_fraction_of_a_yen():
    """The property that actually matters — TSE never quotes sub-yen."""
    for raw in (999.9, 3_000.5, 4_999.99, 29_999.9, 35_617.2, 52_471.05, 299_999.4):
        assert float(_snap_jpy(raw)).is_integer()


def test_ibkr_tick_still_wins_when_it_is_the_coarser_one():
    """max(), not override: a venue we have not tabled must keep trusting IBKR."""
    assert _round_to_tick(100.07, max(0.05, _venue_band_tick("USD", 100.07)), "BUY") == 100.05


def test_untabled_venues_are_left_alone():
    """Returning 0 means 'no opinion' — the caller falls back to IBKR's minTick."""
    for ccy in ("USD", "EUR", "GBP", "CAD", "HKD", "AUD", None):
        assert _venue_band_tick(ccy, 1_404.69) == 0.0


def test_band_tick_is_conservative_so_a_coarse_price_is_legal_on_a_finer_grid():
    """We use the non-TOPIX100 table; a multiple of Y100 is also a multiple of the TOPIX100 Y50."""
    snapped = _snap_jpy(52_471.05)
    assert snapped % 100 == 0 and snapped % 50 == 0


def test_snapped_buy_never_pays_more_than_asked():
    for raw in (35_617.2, 52_471.05, 3_858.77):
        assert _snap_jpy(raw) <= raw


# ── 4. IBKR's market rule is the authoritative banded table ──────────────────
# ContractDetails.marketRuleIds is positionally aligned with validExchanges, and
# reqMarketRule(id) returns the venue's real (lowEdge, increment) bands. That is what minTick
# should have been. Covers EVERY currency without a hardcoded table per venue.

from src.portfolio.buyer import _market_rule_tick, _MARKET_RULE_CACHE


class _Inc:
    def __init__(self, low, inc):
        self.lowEdge, self.increment = low, inc


class _Details:
    def __init__(self, rules, exchanges):
        self.marketRuleIds, self.validExchanges = rules, exchanges


class _RuleIB:
    """Serves a TSE-shaped banded table for rule 32, a flat 0.01 grid for rule 7."""
    def __init__(self):
        self.asked = []

    def reqMarketRule(self, rid):
        self.asked.append(rid)
        if rid == 32:
            return [_Inc(0, 1), _Inc(3000, 5), _Inc(5000, 10),
                    _Inc(30000, 50), _Inc(50000, 100)]
        return [_Inc(0, 0.01)]


def _fresh(monkeypatch):
    _MARKET_RULE_CACHE.clear()
    from src.portfolio import buyer as B
    monkeypatch.setattr(B, "get_portfolio_lock",
                        lambda: __import__("contextlib").nullcontext())
    return _RuleIB()


def test_market_rule_picks_the_band_for_this_price(monkeypatch):
    ib = _fresh(monkeypatch)
    d = _Details("32,7", "TSEJ,SMART")
    assert _market_rule_tick(ib, [d], "TSEJ", 52_471.05) == 100
    assert _market_rule_tick(ib, [d], "TSEJ", 35_617.20) == 50
    assert _market_rule_tick(ib, [d], "TSEJ", 4_000) == 5


def test_market_rule_is_selected_by_the_ROUTED_exchange(monkeypatch):
    """Wrong rule = wrong grid. SMART must get rule 7, not TSEJ's 32."""
    ib = _fresh(monkeypatch)
    d = _Details("32,7", "TSEJ,SMART")
    assert _market_rule_tick(ib, [d], "SMART", 52_471.05) == 0.01
    assert _market_rule_tick(ib, [d], "TSEJ", 52_471.05) == 100


def test_market_rule_result_is_cached(monkeypatch):
    ib = _fresh(monkeypatch)
    d = _Details("32", "TSEJ")
    _market_rule_tick(ib, [d], "TSEJ", 52_471.05)
    _market_rule_tick(ib, [d], "TSEJ", 35_617.20)
    assert ib.asked == [32]                     # one round trip, not two


def test_market_rule_absent_returns_zero_not_a_guess(monkeypatch):
    ib = _fresh(monkeypatch)
    assert _market_rule_tick(ib, [_Details("", "")], "TSEJ", 1000) == 0.0
    assert _market_rule_tick(ib, None, "TSEJ", 1000) == 0.0


def test_market_rule_covers_currencies_with_no_hardcoded_table(monkeypatch):
    """HKD/GBP/AUD/ZAR/EUR have no _venue_band_tick entry — the rule supplies the grid."""
    ib = _fresh(monkeypatch)
    d = _Details("32", "SEHK")
    assert _venue_band_tick("HKD", 52_471.05) == 0.0      # no table
    assert _market_rule_tick(ib, [d], "SEHK", 52_471.05) == 100   # rule still knows
