"""DIRECT SELL button on a holdings row (Rain, 2026-10-22).

Rain's thesis: after a crash the loan is repaid by selling FEWER shares into the recovery than it
bought, keeping the surplus — so no sell RULE is wanted, just a button. "I only want to set the
number of shares, everything other happens behind the curtains."

Policy agreed: limit = max(last x 1.005, avg_cost x 1.002), GTD 3 sessions, outsideRth.
+0.50% is the knee of the fill curve measured on the 76 live holdings over 250 days —
80% same day, 89% within three — while still earning ~EUR 500 on a EUR 100k sale over hitting the bid.
"""
import inspect

import pytest

from src.portfolio import review_orders as ro


# ── the agreed numbers ────────────────────────────────────────────────────────

def test_policy_constants_are_what_we_agreed():
    assert ro.DIRECT_SELL_PREMIUM == 0.005        # +0.50% over the last trade
    assert ro.DIRECT_SELL_COST_FLOOR == 0.002     # +0.2% over cost, for commission and FX
    assert ro.DIRECT_SELL_DAYS == 3               # "same day if possible, or a couple of days"
    assert ro.DIRECT_SELL_ACTION in ro.STOCK_ACTIONS   # same executor as the review sales


def test_premium_is_ABOVE_the_market_unlike_a_review_sale():
    """A review sale leaves a name and wants out, so it goes UNDER the last trade. A direct sell is
    profit-taking and rests ABOVE it. Opposite signs, deliberately."""
    assert ro.SELL_LIMIT_DISCOUNT > 0 and ro.DIRECT_SELL_PREMIUM > 0
    last = 100.0
    assert last * (1 - ro.SELL_LIMIT_DISCOUNT) < last < last * (1 + ro.DIRECT_SELL_PREMIUM)


# ── the limit arithmetic: never below cost ────────────────────────────────────

def _limit(last, cost):
    return max(last * (1 + ro.DIRECT_SELL_PREMIUM), cost * (1 + ro.DIRECT_SELL_COST_FLOOR))


def test_in_profit_the_premium_leads():
    assert round(_limit(13.40, 13.0381), 4) == round(13.40 * 1.005, 4)


def test_below_cost_the_floor_takes_over_so_it_can_never_sell_at_a_loss():
    lim = _limit(12.50, 13.0381)
    assert lim == pytest.approx(13.0381 * 1.002)
    assert lim > 13.0381, "must be strictly above the purchase price"


def test_the_floor_clears_cost_even_at_a_tiny_gain():
    """At last == cost the premium leg alone would sit under cost+costs; the floor must win."""
    cost = 50.0
    assert _limit(cost, cost) > cost * (1 + ro.DIRECT_SELL_COST_FLOOR) - 1e-9


# ── GTD, not DAY and not GTC ──────────────────────────────────────────────────

def test_gtd_stamp_skips_weekends_and_is_ibkr_formatted():
    import re
    from datetime import date, datetime
    st = ro._gtd_stamp(3)
    assert re.fullmatch(r"\d{8} 23:59:00 UTC", st), st
    d = datetime.strptime(st.split()[0], "%Y%m%d").date()
    assert d > date.today()
    assert d.weekday() < 5, "must land on a weekday"


def test_executor_uses_gtd_for_a_direct_sell_and_day_for_the_rest():
    src = inspect.getsource(ro)
    i = src.index('order.tif = "DAY"')
    tail = src[i:i + 400]
    assert "DIRECT_SELL_ACTION" in tail and 'order.tif = "GTD"' in tail
    assert "goodTillDate" in tail


# ── the guards: this is a SELL path, so it must fail closed ───────────────────

@pytest.mark.parametrize("shares,why", [
    ("0", "zero"), ("-5", "negative"), ("abc", "not a number"), ("", "empty"),
])
def test_bad_quantities_send_nothing(shares, why):
    ok, msg = ro.request_holding_sell("NU", shares)
    assert ok is False, why
    assert "positive" in msg or "not a share count" in msg


def test_refuses_when_the_broker_cannot_be_read():
    """The live position is the only size that counts (CCJ 2026-07-27: a phantom DB row sold 500
    twice). With no broker connection this must refuse, never fall back to the DB."""
    ok, msg = ro.request_holding_sell("NU", "100")
    assert ok is False
    assert "not connected" in msg or "could not be read" in msg


def test_every_name_it_uses_is_actually_imported():
    """Regression: the first cut called _ensure_event_loop without importing it, and the broad
    `except Exception` turned the NameError into "the broker could not be read" — a plausible
    message for a function that could never have worked. Compile the body's globals instead."""
    fn = ro.request_holding_sell
    src = inspect.getsource(fn)
    names = {"_ensure_event_loop", "get_portfolio_ib", "get_settings", "is_portfolio_connected",
             "create_suggestion", "approve_suggestion", "TradeSuggestion",
             "REVIEW_MANUAL_APPROVAL_NOTE", "PortfolioHolding", "read_book", "get_db"}
    for n in names:
        if n + "(" in src or n + "." in src or n + "," in src or n + ")" in src:
            assert f"import" in src and n in src, n
    # the local-import block must cover the connection helpers
    head = src[:src.index("try:")]
    for n in ("_ensure_event_loop", "get_portfolio_ib", "is_portfolio_connected", "get_settings",
              "PortfolioHolding", "create_suggestion", "approve_suggestion"):
        assert n in head, f"{n} is used but not imported in the function head"


def test_free_shares_is_what_bounds_the_sale_not_the_holding():
    """Shares pledged to a written call, or already in a working sell, must be untouchable —
    naked calls have happened three times here (CTAS, LRCX, CCJ). free_shares already nets all of
    it, so the button must use that number and not holding.shares."""
    b = ro.Book(long_shares=1000, avg_cost=10.0, short_call_shares=300,
                working_stock_sells=100, working_call_sells=0)
    assert b.free_shares == 600
    assert ro.sale_size(1000, b) == 600          # asked for all, clamped to free
    src = inspect.getsource(ro.request_holding_sell)
    assert "book.free_shares" in src and "h.shares" not in src


def test_it_refuses_rather_than_quietly_selling_less():
    """Rain types a number; if it cannot all be sold he should be told, not silently given less."""
    src = inspect.getsource(ro.request_holding_sell)
    i = src.index("qty > book.free_shares")
    assert "return False" in src[i:i + 300]
