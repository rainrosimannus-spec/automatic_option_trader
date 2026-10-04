"""Candidate pools must use the BROKER'S spelling of venues and tickers.

On 2026-10-04 a check of every pool entry against the broker found 105 of 548 that it did not
recognise — so they had never been scored: all of Switzerland and Denmark (wrong exchange code),
all of Korea (wrong code), most Swedish class shares and Hong Kong tickers (wrong ticker format),
plus renamed, moved and delisted companies. These tests pin the conventions so they cannot drift
back; they do not need the broker.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
su = pytest.importorskip("screen_universe")

POOLS = {**su.CANDIDATE_POOLS, **su.DIVIDEND_CANDIDATES}


def _entries():
    for region, pool in POOLS.items():
        for s in pool["symbols"]:
            yield region, pool["exchange"], pool["currency"], str(s)


def test_exchange_codes_are_the_brokers():
    codes = {(p["exchange"], p["currency"]) for p in POOLS.values()}
    for wrong in ("SWX", "CSE", "KSE", "ISE", "BVMF", "IDX"):
        assert wrong not in {c for c, _ in codes}, f"{wrong} is not a code the broker recognises"
    assert ("EBS", "CHF") in codes and ("CPH", "DKK") in codes and ("KRX", "KRW") in codes


def test_share_classes_use_a_dot_not_a_dash():
    bad = [(r, s) for r, e, c, s in _entries() if re.search(r"-[A-Z]$", s)]
    assert not bad, f"share class must follow a dot (NOVO.B, ATCO.A): {bad}"


def test_hong_kong_tickers_have_no_leading_zeros():
    bad = [s for r, e, c, s in _entries() if e == "SEHK" and s.startswith("0")]
    assert not bad, f"the broker writes 700, not 0700: {bad}"


def test_indian_tickers_fit_the_brokers_nine_characters():
    assert not [s for r, e, c, s in _entries() if e == "NSE" and len(s) > 9]


def test_every_venue_code_is_ranked_for_tradability():
    for region, pool in POOLS.items():
        assert su._venue_rank(pool["exchange"], pool["currency"]) < len(su.VENUE_TRADABILITY), (
            f"{region}: {pool['exchange']}/{pool['currency']} missing from VENUE_TRADABILITY")


def test_every_pool_currency_has_trading_hours():
    # buyer._MARKET_HOURS: a currency missing there is treated as always open.
    import src.portfolio.buyer as buyer
    missing = {p["currency"] for p in POOLS.values()} - set(buyer._MARKET_HOURS)
    assert not missing, f"no trading hours for {missing}"


@pytest.mark.parametrize("gone", ["SQ", "WBA", "MMP", "AVST", "CNHI", "SKG", "FLT", "SCHA", "AMXL",
                                   "ROG", "NZYM-B", "UNA", "0005", "VALE3", "BBCA", "CPG"])
def test_renamed_moved_and_delisted_tickers_are_gone(gone):
    assert gone not in {s for _, _, _, s in _entries()}


@pytest.mark.parametrize("ticker,exchange,currency", [
    ("XYZ", "SMART", "USD"), ("CHKP", "SMART", "USD"), ("CNH", "SMART", "USD"), ("SW", "SMART", "USD"),
    ("FLUT", "SMART", "USD"), ("ITUB", "SMART", "USD"), ("TLK", "SMART", "USD"),
    ("ROP", "EBS", "CHF"), ("NESN", "EBS", "CHF"), ("NOVO.B", "CPH", "DKK"), ("NSIS.B", "CPH", "DKK"),
    ("ATCO.A", "SFB", "SEK"), ("700", "SEHK", "HKD"), ("005930", "KRX", "KRW"),
    ("RYA", "SMART", "EUR"), ("AMP2", "BVME", "EUR"), ("VEND", "OSE", "NOK"), ("AMXB", "MEXI", "MXN"),
])
def test_corrected_entries_sit_where_the_broker_lists_them(ticker, exchange, currency):
    assert (exchange, currency, ticker) in {(e, c, s) for _, e, c, s in _entries()}


def test_korea_keeps_its_native_exchange_in_routing():
    import src.portfolio.connection as conn
    assert "KRX" in conn._NON_SMART_EXCHANGES


def test_prompts_teach_the_model_the_brokers_spelling():
    for prompt in (su._build_growth_augmentation_prompt([], [], 0.0, set()),
                   su._build_breakthrough_prompt("v2")):
        assert "KSE" not in prompt and "SWX" not in prompt
        assert "NOVO.B" in prompt and "leading zeros" in prompt
