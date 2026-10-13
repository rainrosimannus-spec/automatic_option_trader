"""fill_vs_quote: execution quality of an option fill against the decision-time quote,
plus the option feed tracker (live vs delayed) that stamps each log line."""
from types import SimpleNamespace

from src.broker.trade_sync import fill_vs_quote
from src.broker.market_data import feed_name, note_option_feed, option_feed
from src.core.models import TradeType


def test_sell_at_mid_captures_half_the_spread():
    m = fill_vs_quote(TradeType.SELL_PUT, fill=2.10, bid=2.00, ask=2.20, mid=2.10)
    assert m == {"spread": 0.2, "fill_minus_mid": 0.0, "fill_minus_bid": 0.1, "capture_pct": 50.0}


def test_sell_at_bid_captures_nothing_and_above_ask_exceeds_100():
    assert fill_vs_quote("SELL_CALL", 6.00, 6.00, 8.00, 7.00)["capture_pct"] == 0.0
    assert fill_vs_quote("SELL_CALL", 8.50, 6.00, 8.00, 7.00)["capture_pct"] == 125.0


def test_buy_capture_is_measured_from_the_ask():
    m = fill_vs_quote(TradeType.BUY_CALL, fill=24.99, bid=24.0, ask=25.0, mid=24.5)
    assert m["capture_pct"] == 1.0           # paid within a cent of the ask
    assert m["fill_minus_mid"] == 0.49


def test_mid_defaults_to_quote_midpoint_when_missing():
    m = fill_vs_quote("SELL_PUT", 2.15, 2.00, 2.20, None)
    assert m["fill_minus_mid"] == 0.05


def test_unusable_inputs_return_none():
    assert fill_vs_quote("SELL_PUT", 2.0, None, 2.2, 2.1) is None      # no bid
    assert fill_vs_quote("SELL_PUT", 2.0, 2.3, 2.2, 2.1) is None       # crossed quote
    assert fill_vs_quote("SELL_PUT", 0.0, 2.0, 2.2, 2.1) is None       # no fill
    assert fill_vs_quote("SELL_STOCK", 400.0, 399.0, 401.0, 400.0) is None  # not an option side
    assert fill_vs_quote("SELL_PUT", "x", 2.0, 2.2, 2.1) is None


def test_zero_spread_gives_no_capture_pct_but_keeps_the_rest():
    m = fill_vs_quote("SELL_PUT", 2.0, 2.0, 2.0, 2.0)
    assert m["capture_pct"] is None and m["spread"] == 0.0


def test_feed_names_follow_ibkr_market_data_type_ids():
    assert feed_name(1) == "live" and feed_name(3) == "delayed" and feed_name(4) == "delayed_frozen"
    assert feed_name(None) == "unknown" and feed_name("x") == "unknown" and feed_name(9) == "unknown"


def test_note_option_feed_records_the_last_ticker_type():
    assert note_option_feed(SimpleNamespace(marketDataType=3)) == "delayed"
    assert option_feed() == "delayed"
    assert note_option_feed(SimpleNamespace(marketDataType=1)) == "live"
    assert option_feed() == "live"
    assert note_option_feed(SimpleNamespace()) == "unknown"
