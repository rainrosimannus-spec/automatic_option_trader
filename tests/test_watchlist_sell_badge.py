"""The SELL badge on /watchlist: which review cards earn one.

The badge means "a card proposing to part with this stock is open and waiting on approval". All
three sell-side review actions count — outright, partial, and covered-call (which sells the shares
if exercised); the route differs, the question does not. All are in REVIEW_ONLY_ACTIONS and never
auto-execute. Under-reporting is safe; showing SELL for a card that is gone is not."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.web.routes.watchlist import active_sell_map, badge_reason

NOW = datetime(2026, 9, 1, 12, 0, 0)


def _row(symbol, action="sell_stock_review", status="pending", expires_at=None, rationale=None):
    return SimpleNamespace(symbol=symbol, action=action, status=status, rationale=rationale,
                           expires_at=expires_at if expires_at is not None
                           else NOW + timedelta(hours=6))


def _actions(m):
    """The badge's action-only view — most tests care which card badges, not its wording."""
    return {k: v["action"] for k, v in m.items()}


def test_all_three_sell_side_review_actions_badge():
    m = _actions(active_sell_map([_row("LRCX"), _row("VRT", action="reduce_position_review"),
                                  _row("MSFT", action="sell_covered_call_review")], NOW))
    assert m == {"LRCX": "sell_stock_review", "VRT": "reduce_position_review",
                 "MSFT": "sell_covered_call_review"}


def test_covered_call_review_badges_too():
    # A covered call sells the shares if it is exercised — a different ROUTE to selling, same
    # question for the badge to answer.
    assert _actions(active_sell_map([_row("MSFT", action="sell_covered_call_review")], NOW)) == {
        "MSFT": "sell_covered_call_review"}


def test_buy_side_actions_never_badge():
    for action in ("buy_stock", "sell_put"):
        assert active_sell_map([_row("NVDA", action=action)], NOW) == {}


def test_only_undecided_cards_badge():
    for status in ("pending", "submitted", "approved", "queued"):
        assert _actions(active_sell_map([_row("LRCX", status=status)], NOW)) == {"LRCX": "sell_stock_review"}
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
    m = _actions(active_sell_map([_row("LRCX"), _row("LRCX", action="reduce_position_review")], NOW))
    assert m == {"LRCX": "reduce_position_review"}


def test_badge_carries_the_cards_reason_for_the_tooltip():
    # Rain asked WHY a SELL badge is there (2026-09-09); the card's rationale is the answer, with
    # the writers' "MONTHLY REVIEW: " prefix dropped as hover noise.
    m = active_sell_map([_row("TT", rationale="MONTHLY REVIEW: TT (growth) revenue growth slowing "
                                              "(+7.5% YoY, below 15% threshold).")], NOW)
    assert m["TT"] == {"action": "sell_stock_review",
                       "reason": "TT (growth) revenue growth slowing (+7.5% YoY, below 15% threshold)."}


def test_badge_reason_survives_missing_rationale():
    assert badge_reason(None) == ""
    assert badge_reason("  plain text  ") == "plain text"
    assert active_sell_map([_row("LRCX")], NOW)["LRCX"]["reason"] == ""
