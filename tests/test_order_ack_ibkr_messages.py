"""A rejected/cancelled suggestion order carries IBKR's own message (2026-11-04: two SGX buys
cancelled seconds after acceptance, reason never recorded)."""
from types import SimpleNamespace

from src.portfolio.buyer import _order_ack_fields, _trade_messages


def _trade(msgs):
    return SimpleNamespace(
        orderStatus=SimpleNamespace(permId=382397503, whyHeld=""),
        order=SimpleNamespace(orderId=7),
        contract=SimpleNamespace(conId=15785538, exchange="SGX", primaryExchange="SGX", tradingClass="D05"),
        log=[SimpleNamespace(message=m) for m in msgs])


def test_messages_joined_last_three():
    t = _trade(["", "Order Canceled - reason: price cap", "later note"])
    assert _trade_messages(t) == "Order Canceled - reason: price cap; later note"
    assert _order_ack_fields(t)["ibkr_messages"] == "Order Canceled - reason: price cap; later note"
    assert _order_ack_fields(t)["perm_id"] == 382397503


def test_no_messages_is_empty_string():
    assert _trade_messages(_trade([])) == ""
    bare = _order_ack_fields(SimpleNamespace(orderStatus=None, order=None, contract=None))
    assert bare["ibkr_messages"] == "" and bare["perm_id"] == 0
