"""The SELL badge on /watchlist: which review cards earn one.

The badge means "a card proposing to part with this stock is open and waiting on approval" —
sell_stock_review and reduce_position_review are both in suggestions.REVIEW_ONLY_ACTIONS and never
auto-execute. Under-reporting is safe; showing SELL for a card that is gone is not."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.web.routes.watchlist import active_sell_map

NOW = datetime(2026, 9, 1, 12, 0, 0)


def _row(symbol, action="sell_stock_review", status="pending", expires_at=None):
    return SimpleNamespace(symbol=symbol, action=action, status=status,
                           expires_at=expires_at if expires_at is not None
                           else NOW + timedelta(hours=6))


def test_open_sell_and_reduce_cards_both_badge():
    m = active_sell_map([_row("LRCX"), _row("VRT", action="reduce_position_review")], NOW)
    assert m == {"LRCX": "sell_stock_review", "VRT": "reduce_position_review"}


def test_covered_call_review_is_not_a_stock_sale():
    # Writing a call against a holding is not parting with the shares — no badge.
    assert active_sell_map([_row("MSFT", action="sell_covered_call_review")], NOW) == {}
    # ...nor is anything on the buy side.
    assert active_sell_map([_row("NVDA", action="buy_stock")], NOW) == {}


def test_only_undecided_cards_badge():
    for status in ("pending", "submitted", "approved", "queued"):
        assert active_sell_map([_row("LRCX", status=status)], NOW) == {"LRCX": "sell_stock_review"}
    for status in ("executed", "rejected", "cancelled", "expired"):
        assert active_sell_map([_row("LRCX", status=status)], NOW) == {}


def test_expired_but_unswept_card_shows_no_stale_sell():
    # get_pending_suggestions() sweeps these to 'expired', but it WRITES to do it and must not run
    # on a page load — so the badge filters on the timestamp itself.
    assert active_sell_map([_row("LRCX", expires_at=NOW - timedelta(minutes=1))], NOW) == {}
    assert active_sell_map([_row("LRCX", expires_at=NOW + timedelta(minutes=1))], NOW) != {}
    # A card with no expiry at all stays open (built directly — _row's default IS an expiry).
    assert active_sell_map([SimpleNamespace(symbol="LRCX", action="sell_stock_review",
                                            status="pending", expires_at=None)], NOW) != {}


def test_symbol_case_is_normalised_for_template_lookup():
    assert "LRCX" in active_sell_map([_row("lrcx")], NOW)


def test_latest_card_wins_when_a_name_has_two():
    # Rows arrive oldest-first; a later reduce card supersedes an earlier sell card.
    m = active_sell_map([_row("LRCX"), _row("LRCX", action="reduce_position_review")], NOW)
    assert m == {"LRCX": "reduce_position_review"}
