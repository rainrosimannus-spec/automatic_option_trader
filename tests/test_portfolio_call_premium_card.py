"""/portfolio "Covered Calls" card: premium the portfolio account collected, net, in base currency."""
from types import SimpleNamespace as NS

from src.web.routes.portfolio import summarise_call_premium


def _t(action, amount, ccy="EUR", commission=0.0, symbol="X"):
    return NS(action=action, amount=amount, currency=ccy, commission=commission, symbol=symbol)


def test_net_is_written_minus_bought_back_minus_fees_in_base():
    rates = {"EUR": 1.0, "USD": 0.5, "GBP": 2.0}       # LOCAL→BASE
    rows = [_t("sell_call", 1000.0, "USD", 2.0, "VRT"),   # €500 − €1
            _t("sell_call", 100.0, "GBP", 1.0, "AZN"),    # €200 − €2
            _t("buy_call", 200.0, "USD", 2.0, "VRT"),     # −€100 − €1
            _t("call_expired", 0.0, "USD", 0.0, "ARM"),   # no money
            _t("call_assigned", 50000.0, "USD", 0.0, "TSM")]  # stock delivery, NOT premium
    out = summarise_call_premium(rows, rates, "EUR")
    assert out["gross"] == 700.0
    assert out["paid_back"] == 100.0
    assert out["fees"] == 4.0
    assert out["net"] == 596.0
    assert out["written"] == 2 and out["bought_back"] == 1 and out["names"] == 2


def test_empty_book():
    out = summarise_call_premium([], {"EUR": 1.0}, "EUR")
    assert out["net"] == 0.0 and out["written"] == 0 and out["names"] == 0
