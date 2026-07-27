"""IBKR-truth guard against acting on a phantom (unsynced) stock lot.

A lot sold via the live-exit path (cc_sell_above_assignment) has no covered call,
so check_called_away never closes it and its DB row can read OPEN until trade_sync's
independent position_sync catches up. In that window the CC writer must not write a
naked call, and no sell path may place a phantom re-sell (naked short). Two layers:

  A. wheel._ibkr_share_verdict — the reconcile decision the CC writer applies.
  B. orders.sell_stock — a hard clamp that reads live IBKR positions and refuses /
     clamps every sell to the real long quantity (fail-CLOSED).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import src.strategy.wheel as wheel
import src.broker.orders as orders


# ── Layer A: the reconcile verdict ────────────────────────────────────────────

def test_verdict_ok_when_map_none():
    # Untrusted read (fetch raised → None): proceed on DB, do not skip.
    assert wheel._ibkr_share_verdict(None, "CCJ", 500) == ("ok", 500)


def test_verdict_ok_when_map_empty():
    # Empty {} is ambiguous (possible transient blip) → proceed on DB; sell_stock backstops.
    assert wheel._ibkr_share_verdict({}, "CCJ", 500) == ("ok", 500)


def test_verdict_skip_when_symbol_absent_from_populated_map():
    # This is the CCJ case: IBKR still lists other names, just not CCJ → shares are gone.
    ibkr = {"FTAI": 300, "IREN": 200, "LRCX": 100}
    assert wheel._ibkr_share_verdict(ibkr, "CCJ", 500) == ("skip", None)


def test_verdict_skip_when_below_one_lot():
    assert wheel._ibkr_share_verdict({"CCJ": 50}, "CCJ", 500) == ("skip", None)


def test_verdict_clamp_when_ibkr_holds_fewer():
    # DB thinks 500 (5 lots); IBKR shows 300 → clamp down to 3 lots' worth.
    assert wheel._ibkr_share_verdict({"CCJ": 300}, "CCJ", 500) == ("clamp", 300)


def test_verdict_ok_when_counts_agree():
    assert wheel._ibkr_share_verdict({"CCJ": 500}, "CCJ", 500) == ("ok", 500)


def test_verdict_ok_when_ibkr_holds_more():
    # Never scale UP from IBKR — only ever reconcile down to what the DB lot claims.
    assert wheel._ibkr_share_verdict({"CCJ": 800}, "CCJ", 500) == ("ok", 500)


# ── Layer B: sell_stock hard clamp ────────────────────────────────────────────

def _pos(symbol, qty, sectype="STK", account="U25878705"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, secType=sectype),
        position=qty, account=account,
    )


def _wire_ib(monkeypatch, positions):
    """Point orders.get_ib() at a fake IB whose .positions() returns `positions`,
    and neutralize the account filter + order plumbing."""
    placed = {}

    class FakeIB:
        def positions(self):
            return positions
        def qualifyContracts(self, c):
            return [c]
        def placeOrder(self, contract, order):
            placed["contract"] = contract
            placed["order"] = order
            return SimpleNamespace(
                orderStatus=SimpleNamespace(status="Filled"),
                log=[SimpleNamespace(message="")],
            )
        def sleep(self, *_a):
            pass

    import contextlib
    monkeypatch.setattr(orders, "get_ib", lambda: FakeIB())
    monkeypatch.setattr(orders, "get_ib_lock", lambda: contextlib.nullcontext())
    # account filter resolves to our test account
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "get_settings",
                        lambda: SimpleNamespace(ibkr=SimpleNamespace(account="U25878705")))
    return placed


def test_sell_stock_refused_when_flat(monkeypatch):
    # IBKR shows no CCJ long → the phantom-DB re-sell must be refused (no naked short).
    placed = _wire_ib(monkeypatch, [_pos("RACE", 100), _pos("IREN", 200)])
    trade = orders.sell_stock("CCJ", shares=500, limit_price=89.60)
    assert trade is None
    assert "order" not in placed          # nothing was placed


def test_sell_stock_clamps_to_held(monkeypatch):
    # DB asks 500, IBKR holds 300 → sell exactly 300, never more than held.
    placed = _wire_ib(monkeypatch, [_pos("CCJ", 300)])
    trade = orders.sell_stock("CCJ", shares=500, limit_price=89.60)
    assert trade is not None
    assert placed["order"].totalQuantity == 300


def test_sell_stock_full_when_held(monkeypatch):
    placed = _wire_ib(monkeypatch, [_pos("CCJ", 500)])
    trade = orders.sell_stock("CCJ", shares=500, limit_price=89.60)
    assert trade is not None
    assert placed["order"].totalQuantity == 500


def test_sell_stock_ignores_other_accounts(monkeypatch):
    # A same-symbol long in a DIFFERENT account must not count toward held → still refused.
    placed = _wire_ib(monkeypatch, [_pos("CCJ", 500, account="U99999999")])
    trade = orders.sell_stock("CCJ", shares=500, limit_price=89.60)
    assert trade is None
    assert "order" not in placed


def test_sell_stock_failclosed_on_read_error(monkeypatch):
    # If reading positions raises, refuse the sell (never sell on an unverifiable state).
    import contextlib

    class BoomIB:
        def qualifyContracts(self, c):
            return [c]
        def positions(self):
            raise RuntimeError("ib down")

    monkeypatch.setattr(orders, "get_ib", lambda: BoomIB())
    monkeypatch.setattr(orders, "get_ib_lock", lambda: contextlib.nullcontext())
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "get_settings",
                        lambda: SimpleNamespace(ibkr=SimpleNamespace(account="U25878705")))
    trade = orders.sell_stock("CCJ", shares=500, limit_price=89.60)
    assert trade is None
