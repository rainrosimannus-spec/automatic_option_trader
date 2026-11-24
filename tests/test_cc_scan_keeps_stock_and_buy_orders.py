"""2026-11-24 ADSK: the covered-call scan's pre-sweep cancelled the package-unwind lot sale.

15:01 UTC the unwind bought the 260C back (−$879) and placed SELL 100 ADSK @ 261.61; the
16:10-Lux CC scan's "cancel unfilled orders before writing new ones" sweep cancelled it at
15:10. The put scan's sweep had the STK/BUY guards since the ISRG case (2026-07-13); the CC
scan's sweep did not. Now both do, through _cc_sweep_may_cancel.
"""
from types import SimpleNamespace

from src.scheduler import jobs


def _t(sec_type, action, client_id=12, symbol="ADSK", order_id=301259):
    return SimpleNamespace(order=SimpleNamespace(clientId=client_id, orderId=order_id, action=action),
                           contract=SimpleNamespace(symbol=symbol, secType=sec_type))


def test_stock_sell_is_kept():
    assert jobs._cc_sweep_may_cancel(_t("STK", "SELL")) is False      # the ADSK unwind sale


def test_buy_to_close_is_kept():
    assert jobs._cc_sweep_may_cancel(_t("OPT", "BUY")) is False       # CC buyback / put buyback


def test_stale_cc_sell_is_still_cancelled():
    assert jobs._cc_sweep_may_cancel(_t("OPT", "SELL")) is True


def test_unknown_shape_keeps_old_behaviour():
    bare = SimpleNamespace(order=SimpleNamespace(clientId=12, orderId=1),
                           contract=SimpleNamespace(symbol="X"))
    assert jobs._cc_sweep_may_cancel(bare) is True


def test_sweep_site_uses_the_guard():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/scheduler/jobs.py").read_text()
    i = src.index('log.info("cc_scan_cancelling_unfilled_order"')
    block = src[i - 1200:i]
    assert "_cc_sweep_may_cancel(oo)" in block and "cc_scan_keeping_order" in block
