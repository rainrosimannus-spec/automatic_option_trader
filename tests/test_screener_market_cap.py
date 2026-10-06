"""The screen's market cap is the broker's real figure, in dollars.

From March to October 2026 the lookup searched the wrong section of the broker report, never
found the figure, and every name got the fallback guess: price x 100M shares. Apple read as $33B,
and every stock priced under $10 — Itau, Bradesco, Ambev, Lloyds, Barclays, Vodafone, Singtel,
19 of 476 pool names — was rejected at the $1B floor whatever its real size.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
su = pytest.importorskip("screen_universe")


def _snapshot(mktcap="4869932.00000", ccy="USD", group="Income Statement"):
    """The shape IBKR really sends: MKTCAP sits under 'Income Statement', not 'Price and Volume'."""
    cap = f'<Ratio FieldName="MKTCAP" Type="N">{mktcap}</Ratio>' if mktcap is not None else ""
    return f"""<ReportSnapshot><Ratios PriceCurrency="{ccy}" ReportingCurrency="{ccy}">
      <Group ID="Price and Volume"><Ratio FieldName="NPRICE" Type="N">333.69</Ratio></Group>
      <Group ID="{group}">{cap}<Ratio FieldName="TTMREV" Type="N">1.0</Ratio></Group>
    </Ratios></ReportSnapshot>"""


class _IB:
    def __init__(self, xml=None, raises=None):
        self.xml, self.raises = xml, raises

    def reqFundamentalData(self, contract, report):
        if self.raises:
            raise self.raises
        return self.xml


def _stock(symbol="AAPL", currency="USD"):
    return SimpleNamespace(symbol=symbol, currency=currency)


def test_the_figure_is_found_where_the_broker_actually_puts_it():
    assert su._snapshot_market_cap(_snapshot()) == (4869932.0, "USD")
    assert su._snapshot_market_cap(_snapshot(group="Other Ratios")) == (4869932.0, "USD")   # any group


def test_no_figure_or_unreadable_report_gives_nothing():
    assert su._snapshot_market_cap(_snapshot(mktcap=None)) == (None, None)
    assert su._snapshot_market_cap(None) == (None, None)
    assert su._snapshot_market_cap("<not xml") == (None, None)
    assert su._snapshot_market_cap(_snapshot(mktcap="0")) == (None, None)


def test_apple_is_trillions_not_price_times_a_hundred_million():
    cap = su.UniverseScreener(_IB(_snapshot()))._estimate_market_cap(_stock(), price=333.69)
    assert cap == pytest.approx(4.869932e12)
    assert cap != pytest.approx(333.69 * 1e8)


def test_a_large_company_with_a_low_share_price_clears_the_one_billion_floor():
    # Itau: $8.59 a share, $99B company. The old guess made it $0.86B and the floor rejected it.
    cap = su.UniverseScreener(_IB(_snapshot(mktcap="99231.02")))._estimate_market_cap(_stock("ITUB"), price=8.59)
    assert cap == pytest.approx(99.231e9) and cap >= 1e9
    assert 8.59 * 1e8 < 1e9


def test_foreign_listing_is_converted_to_dollars(monkeypatch):
    monkeypatch.setattr(su, "_usd_per_unit", lambda ib, ccy: {"JPY": 0.0063}[ccy])
    scr = su.UniverseScreener(_IB(_snapshot(mktcap="4349432.0", ccy="JPY")))
    cap = scr._estimate_market_cap(_stock("6920", "JPY"), price=46130)
    assert cap == pytest.approx(4349432e6 * 0.0063)          # ~$27B, not the old "$4.6 trillion"


def test_falls_back_to_the_old_guess_only_when_there_is_nothing_better(monkeypatch):
    scr = su.UniverseScreener(_IB(_snapshot(mktcap=None)))
    assert scr._estimate_market_cap(_stock(), price=50.0) == 50.0 * 1e8             # no figure
    scr = su.UniverseScreener(_IB(raises=TimeoutError()))
    assert scr._estimate_market_cap(_stock(), price=50.0) == 50.0 * 1e8             # report failed
    monkeypatch.setattr(su, "_usd_per_unit", lambda ib, ccy: None)
    scr = su.UniverseScreener(_IB(_snapshot(mktcap="1000.0", ccy="XXX")))
    assert scr._estimate_market_cap(_stock("ODD", "XXX"), price=50.0) == 50.0 * 1e8  # no rate


def test_dollar_rate_uses_the_brokers_rates_then_the_rough_table(monkeypatch):
    import src.portfolio.buyer as buyer
    import src.portfolio.fx as fx
    monkeypatch.setattr(su, "_usd_per_unit_memo", {})
    monkeypatch.setattr(fx, "load_fx_rates", lambda: {"EUR": 1.0, "USD": 0.89, "GBP": 1.18})
    monkeypatch.setattr(buyer, "resolve_fx_rate",
                        lambda ib, ccy, base: {"USD": 0.89, "GBP": 1.18}.get(ccy))
    assert su._usd_per_unit(None, "USD") == 1.0
    assert su._usd_per_unit(None, "GBP") == pytest.approx(1.18 / 0.89)
    assert su._usd_per_unit(None, "KRW") == su._APPROX_USD_PER_UNIT["KRW"]          # broker has none
    assert su._usd_per_unit(None, "XXX") is None
