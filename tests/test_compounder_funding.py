"""Fail-closed funding guarantees for the compounder buy path (audit fix #2).

_ensure_currency_funding (FX leg) and _unpark_yield (SGOV unpark) must return False when the
AUTHORITATIVE funding path for a buy could not be satisfied, so the caller skips the order rather
than silently opening a margin loan. No-op / already-funded cases must return True (don't block)."""
from types import SimpleNamespace

from src.portfolio.buyer import _ensure_currency_funding, _unpark_yield


class _OrderStatus:
    def __init__(self, status, filled):
        self.status = status
        self.filled = filled


class _Trade:
    def __init__(self, status="Filled", filled=None):
        self.orderStatus = _OrderStatus(status, filled)


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
    def __init__(self, cash=None, sgov_shares=0, sgov_price=100.0, fill_status="Filled"):
        self._cash = cash or []                       # list of (tag, ccy, value)
        self._sgov_shares = sgov_shares
        self._sgov_price = sgov_price
        self._fill_status = fill_status
        self.orders_placed = 0

    def accountValues(self):
        return [_Val(t, c, v) for (t, c, v) in self._cash]

    def positions(self):
        return [_Pos("SGOV", self._sgov_shares)] if self._sgov_shares else []

    def qualifyContracts(self, *a, **k):
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


def test_unpark_same_ccy_cash_already_covers_true_no_sell():
    # USD buy, USD cash already covers → no need to sell SGOV
    ib = FakeIB(cash=[("CashBalance", "USD", 60_000)], sgov_shares=1000)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 0


def test_unpark_same_ccy_short_sell_fills_true():
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000,
                sgov_price=100.0, fill_status="Filled")
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
    assert ib.orders_placed == 1


def test_unpark_same_ccy_short_sell_unfilled_is_fatal_false():
    # USD buy funded by SGOV proceeds; the sell didn't fill → would draw USD margin → fail-closed
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=1000,
                sgov_price=100.0, fill_status="Submitted")
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is False
    assert ib.orders_placed == 1


def test_unpark_foreign_buy_failure_is_nonfatal_true():
    # GBP buy: the FX leg is the real funding gate, so an SGOV hiccup must NOT block (best-effort)
    ib = FakeIB(sgov_shares=1000, sgov_price=100.0, fill_status="Submitted")
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="GBP") is True


def test_unpark_nothing_parked_true():
    ib = FakeIB(cash=[("CashBalance", "USD", 0)], sgov_shares=0)
    assert _unpark_yield(ib, _cfg(), 50_000, settle_ccy="USD") is True
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
