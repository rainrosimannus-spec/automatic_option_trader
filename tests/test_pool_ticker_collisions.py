"""One ticker = one listing in the candidate pools.

The system keys scores, watchlist rows, holdings and orders on the bare ticker. Until 2026-10-04
twelve tickers sat on two or three venues; five of those were two DIFFERENT companies sharing a
ticker (Merck & Co / Merck KGaA, Suncor / Schneider Electric, Santander / Sanofi, Prudential
Financial / Prudential plc, Siemens Healthineers / Sonic Healthcare), so one company could end up
carrying the other's scores. Rain: "it falls to one, most tradable venue."
"""
import collections
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
su = pytest.importorskip("screen_universe")


def _venues_by_ticker(*universes):
    where = collections.defaultdict(set)
    for u in universes:
        for pool in u.values():
            for s in pool["symbols"]:
                where[str(s)].add((pool["exchange"], pool["currency"]))
    return where


def test_static_pools_have_no_ticker_on_two_venues():
    # The hand-kept pools themselves must be clean, so the run-time guard never has to choose.
    where = _venues_by_ticker(su.CANDIDATE_POOLS, su.DIVIDEND_CANDIDATES)
    collisions = {s: sorted(v) for s, v in where.items() if len(v) > 1}
    assert not collisions, (
        f"ticker on more than one venue: {collisions}. Keep the most tradable listing only; if the "
        f"two are different companies, give the other one a listing with its own ticker or drop it.")


def test_live_universes_have_no_ticker_on_two_venues():
    where = _venues_by_ticker(su._get_growth_universe(), su._get_dividend_universe())
    assert not {s: v for s, v in where.items() if len(v) > 1}


@pytest.mark.parametrize("ticker,venue", [
    ("MRK", ("SMART", "USD")),    # Merck & Co; Merck KGaA dropped
    ("SU", ("SMART", "CAD")),     # Suncor (held); Schneider Electric dropped
    ("SAN", ("BM", "EUR")),       # Santander; Sanofi is SNY
    ("SNY", ("SMART", "USD")),    # Sanofi, US line
    ("PRU", ("SMART", "USD")),    # Prudential Financial
    ("PUK", ("SMART", "USD")),    # Prudential plc, US line
    ("SHL", ("IBIS", "EUR")),     # Siemens Healthineers; Sonic Healthcare dropped
    ("AIR", ("SBF", "EUR")),      # Airbus, primary venue
    ("CRH", ("SMART", "USD")),    # primary listing moved to NYSE
    ("STM", ("SMART", "USD")),    # "STM" does not exist in Paris or Milan at the broker
    ("SHOP", ("SMART", "USD")),
    ("RIO", ("SMART", "USD")),
    ("BHP", ("SMART", "USD")),
    ("SFL", ("SMART", "USD")),
])
def test_each_resolved_ticker_sits_on_its_one_venue(ticker, venue):
    where = _venues_by_ticker(su._get_growth_universe(), su._get_dividend_universe())
    assert where[ticker] == {venue}


def test_us_is_the_most_tradable_venue_and_untradable_ones_rank_last():
    r = su._venue_rank
    assert r("SMART", "USD") == 0
    assert r("SMART", "USD") < r("LSE", "GBP") < r("SBF", "EUR") < r("ASX", "AUD") < r("BVME", "EUR")
    assert r("BVME", "EUR") < r("NSE", "INR") and r("BVME", "EUR") < r("JSE", "ZAR")
    assert r("XXX", "YYY") > r("JSE", "ZAR")          # unknown venue loses to every known one


def test_run_time_collision_falls_to_the_most_tradable_venue():
    growth = {"US": {"exchange": "SMART", "currency": "USD", "symbols": ["AAA", "ONLYUS"]},
              "AU": {"exchange": "ASX", "currency": "AUD", "symbols": ["AAA", "BBB"]},
              "IT": {"exchange": "BVME", "currency": "EUR", "symbols": ["BBB"]}}
    dividend = {"UKD": {"exchange": "LSE", "currency": "GBP", "symbols": ["BBB", "CCC"]}}
    winners = su._ticker_venue_winners(growth, dividend)
    assert winners["AAA"] == ("SMART", "USD") and winners["BBB"] == ("LSE", "GBP")
    g = su._one_venue_per_ticker(growth, winners, "growth")
    d = su._one_venue_per_ticker(dividend, winners, "dividend")
    assert g["US"]["symbols"] == ["AAA", "ONLYUS"]
    assert g["AU"]["symbols"] == [] and g["IT"]["symbols"] == []     # AAA -> US, BBB -> London
    assert d["UKD"]["symbols"] == ["BBB", "CCC"]


def test_a_ticker_listed_twice_on_the_same_venue_is_not_a_collision():
    # XOM is in both the growth and the dividend pool on SMART/USD; that is the same listing.
    growth = {"US": {"exchange": "SMART", "currency": "USD", "symbols": ["XOM"]}}
    dividend = {"US_DIV": {"exchange": "SMART", "currency": "USD", "symbols": ["XOM"]}}
    w = su._ticker_venue_winners(growth, dividend)
    assert su._one_venue_per_ticker(growth, w, "growth")["US"]["symbols"] == ["XOM"]
    assert su._one_venue_per_ticker(dividend, w, "dividend")["US_DIV"]["symbols"] == ["XOM"]
