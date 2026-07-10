"""Fail-closed funding guarantees for the compounder buy path (audit fix #2).

_ensure_currency_funding (FX leg) and _unpark_yield (SGOV unpark) must return False when the
AUTHORITATIVE funding path for a buy could not be satisfied, so the caller skips the order rather
than silently opening a margin loan. No-op / already-funded cases must return True (don't block)."""
from types import SimpleNamespace

import pytest

from src.portfolio import buyer as _buyer
from src.portfolio.buyer import _ensure_currency_funding, _unpark_yield


@pytest.fixture
def park_open(monkeypatch):
    """Pin the park ETF's venue OPEN.

    _unpark_yield refuses to place a sell into a closed venue (a sell that cannot fill frees no cash,
    and unattended retries queued 20 orders that all filled at the next open). Without pinning, every
    unpark test below would pass or fail depending on the wall-clock hour it happened to run at."""
    monkeypatch.setattr(_buyer, "_market_open", lambda ccy: True)


@pytest.fixture
def park_closed(monkeypatch):
    monkeypatch.setattr(_buyer, "_market_open", lambda ccy: False)


class _OrderStatus:
    def __init__(self, status, filled):
        self.status = status
        self.filled = filled


class _Trade:
    def __init__(self, status="Filled", filled=None):
        self.orderStatus = _OrderStatus(status, filled)
        self.order = SimpleNamespace()


class _FxTicker:
    """Minimal ib_insync Ticker stand-in for the FX-rate snapshot."""
    def __init__(self, rate):
        self._rate = rate
        self.last = float("nan")
        self.close = float("nan")

    def midpoint(self):
        return self._rate

    def marketPrice(self):
        return self._rate


class _Bar:
    def __init__(self, close):
        self.close = close


class _Val:
    def __init__(self, tag, currency, value):
        self.tag, self.currency, self.value = tag, currency, value


class _Pos:
    def __init__(self, symbol, position):
        self.contract = SimpleNamespace(symbol=symbol)
        self.position = position


class FakeIB:
    """Configurable IB stub. fill_status drives the placed order's resulting status."""
    def __init__(self, cash=None, sgov_shares=0, sgov_price=100.0, fill_status="Filled",
                 fx_rate=1.0):
        self._cash = cash or []                       # list of (tag, ccy, value)
        self._sgov_shares = sgov_shares
        self._sgov_price = sgov_price
        self._fill_status = fill_status
        self._fx_rate = fx_rate                       # ccy-per-base for the FX-rate snapshot
        self.orders_placed = 0
        self.orders_cancelled = 0

    def accountValues(self):
        return [_Val(t, c, v) for (t, c, v) in self._cash]

    def positions(self):
        return [_Pos("SGOV", self._sgov_shares)] if self._sgov_shares else []

    def qualifyContracts(self, *a, **k):
        # The funding path qualifies the canonical FX pair and checks conId — stamp one so the
        # first-tried (base-first) direction resolves, mirroring IBKR's single-direction pairs.
        for c in a:
            try:
                c.conId = 1
            except Exception:
                pass
        return list(a)

    def reqMktData(self, *a, **k):
        return _FxTicker(self._fx_rate)

    def cancelMktData(self, *a, **k):
        return None

    def cancelOrder(self, *a, **k):
        self.orders_cancelled += 1
        return None

    def reqHistoricalData(self, *a, **k):
        return [_Bar(self._sgov_price)]

    def placeOrder(self, contract, order):
        self.orders_placed += 1
        filled = order.totalQuantity if self._fill_status == "Filled" else 0.0
        return _Trade(self._fill_status, filled)

    def sleep(self, *a, **k):
        return None


def _cfg(**over):
    base = dict(cash_yield_enabled=True, readonly=False,
                cash_yield_symbol="SGOV", cash_yield_currency="USD")
    base.update(over)
    return SimpleNamespace(**base)


# ── _ensure_currency_funding (FX leg) ────────────────────────────────────────────────────

def test_fx_same_currency_is_noop_true():
    ib = FakeIB()
    assert _ensure_currency_funding(ib, "EUR", "EUR", 50_000) is True
    assert ib.orders_placed == 0


def test_fx_already_funded_true_no_order():
    ib = FakeIB(cash=[("CashBalance", "USD", 60_000)])
    assert _ensure_currency_funding(ib, "USD", "EUR", 50_000) is True
    assert ib.orders_placed == 0


def test_fx_submin_shortfall_left_to_loan_true():
    # shortfall 500 < min_convert 1000 → acceptable, proceed without an FX order
    ib = FakeIB(cash=[("CashBalance", "USD", 49_500)])
    assert _ensure_currency_funding(ib, "USD", "EUR", 50_000) is True
    assert ib.orders_placed == 0


def test_fx_real_shortfall_filled_true():
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], fill_status="Filled")
    assert _ensure_currency_funding(ib, "USD", "EUR", 50_000) is True
    assert ib.orders_placed == 1


def test_fx_real_shortfall_unfilled_is_fatal_false():
    # FX market order did not fill → would open a USD margin loan → must fail-closed
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], fill_status="Submitted")
    assert _ensure_currency_funding(ib, "USD", "EUR", 50_000) is False
    assert ib.orders_placed == 1


# ── _unpark_yield (SGOV unpark) ──────────────────────────────────────────────────────────

def test_unpark_disabled_true():
    ib = FakeIB(sgov_shares=1000)
    assert _unpark_yield(ib, _cfg(cash_yield_enabled=False), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 0


def test_unpark_same_ccy_cash_already_covers_true_no_sell(park_open):
    # USD buy, USD cash already covers → no need to sell SGOV
    ib = FakeIB(cash=[("CashBalance", "USD", 60_000)], sgov_shares=1000)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 0


def test_unpark_same_ccy_short_sell_fills_true(park_open):
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000,
                sgov_price=100.0, fill_status="Filled")
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 1


def test_unpark_same_ccy_short_sell_unfilled_is_fatal_false(park_open):
    # USD buy funded by SGOV proceeds; the sell didn't fill → would draw USD margin → fail-closed
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000,
                sgov_price=100.0, fill_status="Submitted")
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is False
    assert ib.orders_placed == 1


def test_unpark_foreign_buy_failure_is_nonfatal_true(park_open):
    # GBP buy: the FX leg is the real funding gate, so an SGOV hiccup must NOT block (best-effort)
    ib = FakeIB(sgov_shares=1000, sgov_price=100.0, fill_status="Submitted")
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="GBP") is True


def test_unpark_nothing_parked_true(park_open):
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=0)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 0


# ── park venue closed: never place a sell that cannot fill ───────────────────────────────
# Regression: 2026-07-10. Foreign buys retried every ~30s all through the Asian session, each one
# firing an unattended MarketOrder SELL into a shut Xetra. Twenty queued and every one filled at the
# 07:00 UTC open — 7,394 XEON shares (~€1.1M) liquidated for buys that never happened, then re-parked
# an hour later. A sell into a closed venue frees no cash for the buy that asked for it, so don't.

def test_unpark_closed_venue_places_nothing_foreign_buy(park_closed):
    ib = FakeIB(cash=[("CashBalance", "EUR", 3_500)], sgov_shares=1000, sgov_price=100.0)
    # foreign buy → non-fatal, caller proceeds to the FX gate (which fails cleanly on its own)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="GBP") is True
    assert ib.orders_placed == 0


def test_unpark_closed_venue_does_not_block_an_already_funded_buy(park_closed):
    """Cash-first wins over the venue gate: a buy that needs no sale must not be skipped just
    because the park is shut. The closed-venue check sits after the cash-sufficiency return."""
    ib = FakeIB(cash=[("CashBalance", "USD", 60_000)], sgov_shares=1000)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 0


def test_unpark_closed_venue_nothing_parked_does_not_block(park_closed):
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=0)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True


def test_unpark_closed_venue_same_ccy_is_fatal(park_closed):
    # USD buy funded by the park's proceeds, park shut → must fail-closed, not draw margin
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000, sgov_price=100.0)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is False
    assert ib.orders_placed == 0


def test_unpark_closed_venue_retries_never_accumulate(park_closed):
    """The actual failure mode: 18 executor retries -> 18 resting sells. Must stay at zero."""
    ib = FakeIB(cash=[("CashBalance", "EUR", 3_500)], sgov_shares=71_098, sgov_price=149.66)
    for _ in range(18):
        _unpark_yield(ib, _cfg(), 10_912_573.0, settle_ccy="JPY")   # ¥10.9M, the real 4385 notional
    assert ib.orders_placed == 0


def test_unpark_unfilled_sell_is_cancelled(park_open):
    """An unfilled sell must not be left resting — it would fill later against a dead buy."""
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000,
                sgov_price=100.0, fill_status="Submitted")
    _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD")
    assert ib.orders_placed == 1
    assert ib.orders_cancelled == 1


def test_unpark_filled_sell_is_not_cancelled(park_open):
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000,
                sgov_price=100.0, fill_status="Filled")
    _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD")
    assert ib.orders_cancelled == 0


def test_unpark_unreadable_cash_line_fails_closed(park_open, monkeypatch):
    """A transient accountValues() error must not read as 'no cash' and liquidate the park."""
    monkeypatch.setattr(_buyer, "_ccy_cash", lambda ib, ccy: None)
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000, sgov_price=100.0)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is False
    assert ib.orders_placed == 0


# ── caller gate selection (authoritative path by currency) ───────────────────────────────

def _funded(ccy, base, fx_ok, unpark_ok):
    """Mirror of the caller's gate: FX for a foreign buy, unpark for a same-currency buy."""
    return fx_ok if (ccy or "").upper() != (base or "EUR").upper() else unpark_ok


def test_gate_picks_fx_for_foreign_buy():
    # foreign buy is gated by FX, not unpark
    assert _funded("USD", "EUR", fx_ok=False, unpark_ok=True) is False
    assert _funded("USD", "EUR", fx_ok=True, unpark_ok=False) is True


def test_gate_picks_unpark_for_same_currency_buy():
    assert _funded("EUR", "EUR", fx_ok=True, unpark_ok=False) is False
    assert _funded("EUR", "EUR", fx_ok=False, unpark_ok=True) is True
