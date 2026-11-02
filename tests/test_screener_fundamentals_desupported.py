"""TWS API 10.47 de-supported reqFundamentalData; gateway 10.50 refuses with Error 10358.

2026-11-02 monthly screen: 4,900 refusals, the price request (generic tick 258) died with them,
nan x 100M shares failed the market-cap floor for 561 names and a 32-name universe overwrote 128.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
su = pytest.importorskip("screen_universe")
from src.portfolio import ibkr_fundamentals as ifd  # noqa: E402


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def fire(self, *a):
        for h in self.handlers:
            h(*a)


class _IB:
    def __init__(self):
        self.errorEvent = _Event()
        self.calls = 0

    def reqFundamentalData(self, contract, report):
        self.calls += 1
        self.errorEvent.fire(self.calls, 10358, "Fundamentals data is not allowed.", contract)
        return ""


def test_first_refusal_stops_all_further_fundamentals_requests():
    ib = _IB()
    c = SimpleNamespace(symbol="AAPL", currency="USD")
    assert ifd.fundamentals_available(ib) is True
    assert ifd.get_ibkr_fundamentals(ib, c, current_price=100.0) == {}
    assert ib.calls == 1
    assert ifd.fundamentals_available(ib) is False
    assert ifd.get_ibkr_quality_fundamentals(ib, c) == {}
    assert ifd.get_ibkr_review_fundamentals(ib, c) == {}
    assert ifd.get_next_earnings_date(ib, c).status == "fetch_failed"
    assert ib.calls == 1  # nothing asked again


def test_other_errors_do_not_flip_the_flag():
    ib = _IB()
    ifd.fundamentals_available(ib)
    ib.errorEvent.fire(1, 200, "No security definition", None)
    assert ifd.fundamentals_available(ib) is True


@pytest.mark.parametrize("v,ok", [(nan := float("nan"), False), (None, False), (0, False), (-1, False),
                                  (12.5, True), ("3.2", True), ("x", False)])
def test_positive_price_rejects_nan(v, ok):
    assert su._positive(v) is ok


@pytest.mark.parametrize("sym,ex,ccy,want", [
    ("AAPL", "SMART", "USD", "AAPL"), ("AAPL", "NASDAQ", "USD", "AAPL"),
    ("ASML", "AEB", "EUR", "ASML.AS"), ("SDF", "IBIS", "EUR", "SDF.DE"), ("ABF", "LSE", "GBP", "ABF.L"),
    ("700", "SEHK", "HKD", "0700.HK"), ("2318", "SEHK", "HKD", "2318.HK"), ("7203", "TSEJ", "JPY", "7203.T"),
    ("PME", "ASX", "AUD", "PME.AX"), ("ZEAL", "CPH", "DKK", "ZEAL.CO"), ("S68", "SGX", "SGD", "S68.SI"),
    ("ESLT", "TASE", "ILS", "ESLT.TA"), ("042660", "KRX", "KRW", "042660.KS"), ("ITC", "NSE", "INR", "ITC.NS"),
    ("CSU", "SMART", "CAD", "CSU.TO"), ("INVE B", "SFB", "SEK", "INVE-B.ST"), ("SU.PA", "SBF", "EUR", "SU.PA"),
])
def test_fmp_symbol_suffixes(sym, ex, ccy, want):
    assert su.fmp_symbol(sym, ex, ccy) == want


def test_fmp_profile_market_cap_converts_listing_currency(monkeypatch):
    seen = {}
    monkeypatch.setattr(su, "_fmp_get", lambda ep, sym, params={}: seen.setdefault("sym", sym) and
                        [{"marketCap": 13_582_100_381, "currency": "GBp"}])
    monkeypatch.setattr(su, "_usd_per_unit", lambda ib, ccy: {"GBP": 1.30}.get(ccy))
    c = SimpleNamespace(symbol="ABF", exchange="LSE", primaryExchange="LSE", currency="GBP")
    assert su._fmp_profile_market_cap_usd(None, c) == pytest.approx(13_582_100_381 * 1.30)
    assert seen["sym"] == "ABF.L"   # pence label, pounds figure: no /100


def test_estimate_market_cap_uses_fmp_when_broker_refuses(monkeypatch):
    ib = _IB()
    scr = su.UniverseScreener.__new__(su.UniverseScreener)
    scr.ib = ib
    monkeypatch.setattr(su, "_fmp_profile_market_cap_usd", lambda ib_, c: 4.9e12)
    c = SimpleNamespace(symbol="AAPL", exchange="SMART", primaryExchange="NASDAQ", currency="USD")
    assert scr._estimate_market_cap(c, 270.0) == 4.9e12
    assert scr._estimate_market_cap(c, 270.0) == 4.9e12
    assert ib.calls == 1  # the broker was asked once, then never again
