"""The covered-call scan's pre-sweep must not try to cancel another API client's order.

2026-10-29..11-02: a hand-placed GTC "SELL 1 XFFE @ 217.90" from client id 19 sat in the
account; the sweep cancelled it every 30 min and IBKR answered Error 10147 each time.
"""
from types import SimpleNamespace

from src.scheduler import jobs


def _trade(client_id, symbol="XFFE", order_id=4):
    return SimpleNamespace(order=SimpleNamespace(clientId=client_id, orderId=order_id),
                           contract=SimpleNamespace(symbol=symbol))


def test_own_order_is_ours(monkeypatch):
    monkeypatch.setattr(jobs, "get_settings",
                        lambda: SimpleNamespace(ibkr=SimpleNamespace(client_id=12)))
    assert jobs._order_is_ours(_trade(12)) is True


def test_foreign_order_is_not_ours(monkeypatch):
    monkeypatch.setattr(jobs, "get_settings",
                        lambda: SimpleNamespace(ibkr=SimpleNamespace(client_id=12)))
    assert jobs._order_is_ours(_trade(19)) is False
    assert jobs._order_is_ours(_trade(0)) is False  # TWS manual orders are not ours either


def test_unknown_client_id_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(jobs, "get_settings",
                        lambda: SimpleNamespace(ibkr=SimpleNamespace(client_id=12)))
    assert jobs._order_is_ours(SimpleNamespace(order=SimpleNamespace(clientId=None, orderId=1),
                                               contract=SimpleNamespace(symbol="X"))) is True
