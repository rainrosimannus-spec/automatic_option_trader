"""SGX rejects a limit more than 30 minimum bids from the last trade (Forced Order Range); IBKR relays
it as a bare Cancelled. 2026-11-04/11: DBS and OCBC leader buys at +1.5% (~110 ticks) died every day.
The buyer now keeps SGX limits within 10 ticks of the reference (25 was refused on 11-12: the
reference print lags 10–20 min and DBS had slid 11 ticks meanwhile)."""
from types import SimpleNamespace

from src.portfolio.buyer import (_sgx_range_clamp, _sgx_tick, _sgx_reference_price,
                                 _SGX_FORCED_ORDER_RANGE_TICKS)


def test_sgx_tick_bands():
    assert _sgx_tick(0.15) == 0.001
    assert _sgx_tick(1.50) == 0.005
    assert _sgx_tick(71.03) == 0.01


def test_dbs_leader_limit_is_clamped_to_10_ticks_above_last():
    price, note = _sgx_range_clamp(72.13, 71.03)          # the 11-11 order: +110 ticks
    assert note == "clamped" and abs(price - 71.13) < 1e-9
    assert _SGX_FORCED_ORDER_RANGE_TICKS == 10


def test_11_12_order_now_lands_inside_the_band():
    # reference print 71.15 (14 min old), live last 71.04: 25 ticks gave 71.40 = 36 above live → refused
    price, note = _sgx_range_clamp(72.22, 71.15)
    assert note == "clamped" and abs(price - 71.25) < 1e-9
    assert round((price - 71.04) / 0.01) <= 30


def test_ocbc_leader_limit_clamped_and_sgx_ltd_bid_untouched():
    price, note = _sgx_range_clamp(28.28, 27.86)           # +42 ticks
    assert note == "clamped" and abs(price - 27.96) < 1e-9
    price, note = _sgx_range_clamp(20.56, 20.63)           # 7 ticks below: rests, as it did
    assert note == "ok" and price == 20.56


def test_bid_far_below_is_refused_not_sent():
    price, note = _sgx_range_clamp(69.50, 71.03)           # −153 ticks: exchange would reject
    assert price is None and note == "below"
    price, note = _sgx_range_clamp(70.94, 71.03)           # −9 ticks: fine
    assert note == "ok"


def test_unknown_reference_leaves_price_alone():
    assert _sgx_range_clamp(72.13, None) == (72.13, "no_reference")
    assert _sgx_range_clamp(72.13, 0.0) == (72.13, "no_reference")


def test_reference_price_daily_bar_then_fallback(monkeypatch):
    import src.portfolio.buyer as b
    from contextlib import contextmanager

    @contextmanager
    def lock():
        yield
    monkeypatch.setattr(b, "get_portfolio_lock", lock)
    ib = SimpleNamespace(reqHistoricalData=lambda *a, **k: [SimpleNamespace(close=71.03)])
    assert _sgx_reference_price(ib, object(), 72.13) == 71.03
    ib_dead = SimpleNamespace(reqHistoricalData=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    assert _sgx_reference_price(ib_dead, object(), 72.13) == 72.13
    ib_empty = SimpleNamespace(reqHistoricalData=lambda *a, **k: [])
    assert _sgx_reference_price(ib_empty, object(), None) is None
