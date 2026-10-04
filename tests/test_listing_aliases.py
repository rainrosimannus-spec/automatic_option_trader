"""Two companies, one broker ticker (src/portfolio/symbols.py LISTING_ALIASES).

"SU" is Suncor in Toronto and Schneider Electric in Paris; "MRK" is Merck & Co and Merck KGaA;
"SHL" is Siemens Healthineers and Sonic Healthcare. The second company of each pair gets an
internal name (SU.PA, MRK.DE, SHL.AX). These tests pin the two translations at the broker
boundary and, above all, the dangerous direction: shares of one company must never be booked
onto the other's row.
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base
from src.portfolio import symbols as sym
from src.portfolio.models import PortfolioHolding, PortfolioWatchlist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

# broker contracts as the broker reports them (contract ids verified by ISIN on 2026-10-04)
SUNCOR_TSE = NS(symbol="SU", secType="STK", currency="CAD", conId=64731259, exchange="TSE", primaryExchange="TSE", localSymbol="SU")
SUNCOR_NYSE = NS(symbol="SU", secType="STK", currency="USD", conId=64731225, exchange="NYSE", primaryExchange="NYSE", localSymbol="SU")
SCHNEIDER = NS(symbol="SU", secType="STK", currency="EUR", conId=29612196, exchange="SBF", primaryExchange="SBF", localSymbol="SU")
MERCK_US = NS(symbol="MRK", secType="STK", currency="USD", conId=70101545, exchange="NYSE", primaryExchange="NYSE", localSymbol="MRK")
MERCK_KGAA = NS(symbol="MRK", secType="STK", currency="EUR", conId=1015559, exchange="IBIS", primaryExchange="IBIS", localSymbol="MRK")
SIEMENS_H = NS(symbol="SHL", secType="STK", currency="EUR", conId=310520441, exchange="IBIS", primaryExchange="IBIS", localSymbol="SHL")
SONIC = NS(symbol="SHL", secType="STK", currency="AUD", conId=12370176, exchange="ASX", primaryExchange="ASX", localSymbol="SHL")


# ── inbound: broker contract -> internal name ────────────────────────────────

@pytest.mark.parametrize("contract,expected", [
    (SUNCOR_TSE, "SU"), (SUNCOR_NYSE, "SU"), (SCHNEIDER, "SU.PA"),
    (MERCK_US, "MRK"), (MERCK_KGAA, "MRK.DE"),
    (SIEMENS_H, "SHL"), (SONIC, "SHL.AX"),
    (NS(symbol="AAPL", secType="STK", currency="USD", conId=265598), "AAPL"),
])
def test_each_contract_maps_to_its_own_company(contract, expected):
    assert sym.internal_symbol(contract) == expected


def test_contract_id_decides_even_when_the_venue_differs():
    # Schneider routed through Xetra or SMART is still the same contract id.
    c = NS(symbol="SU", secType="STK", currency="EUR", conId=29612196, exchange="IBIS")
    assert sym.internal_symbol(c) == "SU.PA"


def test_falls_back_to_ticker_and_currency_when_the_contract_has_no_id():
    assert sym.internal_symbol(NS(symbol="SU", secType="STK", currency="EUR", conId=0)) == "SU.PA"
    assert sym.internal_symbol(NS(symbol="SU", secType="STK", currency="CAD", conId=0)) == "SU"


def test_options_keep_their_underlying_ticker():
    # A put on Merck & Co must not be renamed, whatever currency it carries.
    assert sym.internal_symbol(NS(symbol="MRK", secType="OPT", currency="EUR", conId=1015559)) == "MRK"


def test_empty_registry_is_the_identity(monkeypatch):
    monkeypatch.setattr(sym, "LISTING_ALIASES", {})
    assert sym.internal_symbol(SCHNEIDER) == "SU"
    c = sym.broker_stock("SU.PA", "SBF", "EUR")
    assert c.symbol == "SU.PA" and not c.conId        # nothing translated when nothing is registered


# ── outbound: internal name -> broker contract ───────────────────────────────

def test_ordinary_name_builds_the_same_contract_as_before():
    from ib_insync import Stock
    assert sym.broker_stock("AAPL", "SMART", "USD") == Stock("AAPL", "SMART", "USD")
    assert sym.broker_symbol("AAPL") == "AAPL" and sym.broker_symbol(None) is None


@pytest.mark.parametrize("exchange", ["SBF", "SMART"])
def test_aliased_name_builds_the_real_contract_pinned_by_id(exchange):
    c = sym.broker_stock("SU.PA", exchange, "EUR")
    assert (c.symbol, c.currency, c.conId, c.exchange) == ("SU", "EUR", 29612196, exchange)
    assert sym.broker_symbol("SU.PA") == "SU"


def test_aliased_contract_ignores_a_wrong_currency_from_the_caller():
    # A caller that defaulted to USD must still get Schneider, never Suncor's US line.
    c = sym.broker_stock("SU.PA", "SMART", "USD")
    assert c.currency == "EUR" and c.conId == 29612196


def test_round_trip():
    for name in sym.LISTING_ALIASES:
        a = sym.LISTING_ALIASES[name]
        assert sym.internal_symbol(sym.broker_stock(name, a.exchange, a.currency)) == name


# ── registry sanity ──────────────────────────────────────────────────────────

def test_registry_is_consistent_with_the_pools():
    su = pytest.importorskip("screen_universe")
    ids = [a.con_id for a in sym.LISTING_ALIASES.values()]
    assert len(ids) == len(set(ids)) and all(ids)
    pools = {**su.CANDIDATE_POOLS, **su.DIVIDEND_CANDIDATES}
    for name, a in sym.LISTING_ALIASES.items():
        assert name != a.symbol
        # two kinds of alias: a venue suffix for a shared ticker (SU.PA -> SU), or a broker
        # ticker that ends in a dot (BP -> "BP.")
        assert name.split(".")[0] == a.symbol or a.symbol == name + "."
        # the aliased company sits in a pool on its own venue under its INTERNAL name...
        homes = [r for r, p in pools.items() if name in p["symbols"]]
        assert homes and all((pools[r]["exchange"], pools[r]["currency"]) == (a.exchange, a.currency) for r in homes), name
        # ...and no pool in that currency carries the bare ticker, which would be ambiguous on the
        # ticker+currency fallback.
        assert not [r for r, p in pools.items() if a.symbol in p["symbols"] and p["currency"] == a.currency], name


def test_aliased_names_never_enter_the_options_universe():
    assert sym.is_aliased("SU.PA") and not sym.is_aliased("SU") and not sym.is_aliased(None)
    src = (Path(__file__).resolve().parent.parent / "tools" / "screen_universe.py").read_text()
    assert "s.options_available and not is_aliased(s.symbol)" in src


# ── the dangerous direction, end to end: holdings sync ───────────────────────

@pytest.fixture
def holdings(monkeypatch):
    import src.portfolio.sync as sync_mod
    import src.portfolio.connection as conn_mod
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()

    @contextmanager
    def fake_db():
        yield sess

    @contextmanager
    def fake_lock():
        yield
    monkeypatch.setattr(sync_mod, "get_db", fake_db)
    monkeypatch.setattr(conn_mod, "get_portfolio_lock", fake_lock)

    def run(items):
        ib = NS(reqPositions=lambda: None, sleep=lambda s: None, portfolio=lambda: items,
                positions=lambda: [])
        sync_mod.sync_ibkr_holdings(ib)
        return {h.symbol: h for h in sess.query(PortfolioHolding).all()}
    return NS(run=run, sess=sess)


def _item(contract, shares, cost):
    return NS(contract=contract, position=shares, averageCost=cost, marketPrice=cost,
              marketValue=shares * cost, unrealizedPNL=0.0)


def test_suncor_and_schneider_get_separate_holdings_rows(holdings):
    rows = holdings.run([_item(SUNCOR_TSE, 700, 50.0), _item(SCHNEIDER, 120, 240.0)])
    assert rows["SU"].shares == 700 and rows["SU"].currency == "CAD"
    assert rows["SU.PA"].shares == 120 and rows["SU.PA"].currency == "EUR"


def test_selling_all_of_one_does_not_touch_the_other(holdings):
    holdings.run([_item(SUNCOR_TSE, 700, 50.0), _item(SCHNEIDER, 120, 240.0)])
    rows = holdings.run([_item(SUNCOR_TSE, 700, 50.0)])            # Schneider position gone
    assert rows["SU"].shares == 700 and rows["SU.PA"].shares == 0
    rows = holdings.run([_item(SCHNEIDER, 120, 240.0)])            # and the other way round
    assert rows["SU"].shares == 0 and rows["SU.PA"].shares == 120


def test_schneider_row_takes_its_tier_from_its_own_watchlist_entry(holdings):
    holdings.sess.add(PortfolioWatchlist(symbol="SU", name="SUNCOR ENERGY INC", tier="dividend", currency="CAD"))
    holdings.sess.add(PortfolioWatchlist(symbol="SU.PA", name="SCHNEIDER ELECTRIC SE", tier="growth", currency="EUR"))
    holdings.sess.flush()
    rows = holdings.run([_item(SUNCOR_TSE, 700, 50.0), _item(SCHNEIDER, 120, 240.0)])
    assert rows["SU"].tier == "dividend" and rows["SU.PA"].tier == "growth"
    assert rows["SU.PA"].name == "SCHNEIDER ELECTRIC SE"


def test_tickers_the_broker_ends_with_a_dot_use_a_clean_internal_name():
    c = sym.broker_stock("BP", "LSE", "GBP")
    assert (c.symbol, c.currency, c.conId) == ("BP.", "GBP", 228891)
    assert sym.internal_symbol(NS(symbol="BP.", secType="STK", currency="GBP", conId=228891)) == "BP"
    assert sym.internal_symbol(NS(symbol="NG.", secType="STK", currency="GBP", conId=0)) == "NG"
