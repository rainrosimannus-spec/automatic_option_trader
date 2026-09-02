"""Portfolio venue policy — reroute or drop names whose home market this account can't trade.

Background: the compounder starred FSR (FirstRand, JSE) as its next buy for weeks. Every
attempt was rejected by IBKR with Error 460 "No trading permissions" — and because the
currency is funded BEFORE the order is acknowledged, one of those attempts converted
EUR 29,097 into rand for a purchase that could never happen. The name also kept its slot
in the roster, so its share of the target was never deployed at all.

The fix screens such names out at source: a blocked-currency name is rerouted to a US
listing of the SAME company, or dropped from the universe. Every description string below
is verbatim from IBKR's symbol search — that truncation and abbreviation is exactly what
the matcher has to survive without ever matching the wrong company.
"""
import pytest

import tools.screen_universe as T


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    """resolve_us_listing paces itself for IBKR's ~1/sec symbol-search limit."""
    monkeypatch.setattr(T.time, "sleep", lambda *_a, **_k: None)


class _C:
    def __init__(self, symbol, secType="STK", currency="USD", primaryExchange="NYSE",
                 description=""):
        self.symbol, self.secType, self.currency = symbol, secType, currency
        self.primaryExchange, self.description = primaryExchange, description


class _D:
    """One ContractDescription as reqMatchingSymbols returns it."""
    def __init__(self, contract):
        self.contract = contract


class _IB:
    """Symbol search over a fixed table; records what was asked for."""
    def __init__(self, table, boom=False):
        self.table, self.boom, self.patterns = table, boom, []

    def reqMatchingSymbols(self, pattern):
        self.patterns.append(pattern)
        if self.boom:
            raise RuntimeError("IBKR: max number of requests exceeded")
        return [_D(c) for c in self.table if pattern.upper() in
                (c.description.upper() + " " + c.symbol.upper())]


def _ds(*contracts):
    """reqMatchingSymbols returns ContractDescriptions, not contracts — wrap like the API."""
    return [_D(c) for c in contracts]


def _stock(symbol, name, currency, exchange):
    return T.StockScore(symbol=symbol, name=name, currency=currency, exchange=exchange)


# ── name matching ───────────────────────────────────────────────────────────

def test_boilerplate_and_share_class_letters_are_not_identity():
    assert T._company_tokens("FIRSTRAND LTD-UNSPON ADR") == ["FIRSTRAND"]
    assert T._company_tokens("STANDARD BANK GROUP-SPON ADR") == ["STANDARD", "BANK"]
    # 'N' is a share class, 'SHS' boilerplate — Naspers must reduce to its name alone.
    assert T._company_tokens("NASPERS LTD-N SHS SPON ADR") == ["NASPERS"]


@pytest.mark.parametrize("home,adr", [
    ("FIRSTRAND LTD", "FIRSTRAND LTD-UNSPON ADR"),
    ("STANDARD BANK GROUP LTD", "STANDARD BANK GROUP-SPON ADR"),
    ("SASOL LTD", "SASOL LTD-SPONSORED ADR"),
    ("ICICI BANK LTD", "ICICI BANK LTD-SPON ADR"),
    ("NASPERS LTD-N SHS", "NASPERS LTD-N SHS SPON ADR"),
    ("ANGLO AMERICAN PLC", "ANGLO AMERICAN PLC-SPONS ADR"),
    # The truncation case: IBKR cuts descriptions to ~28 chars by abbreviating, so the
    # NYSE line for British American Tobacco reads TOB where the JSE line reads TOBACCO.
    ("BRITISH AMERICAN TOBACCO PLC", "BRITISH AMERICAN TOB-SP ADR"),
])
def test_the_same_company_is_recognised_through_adr_boilerplate(home, adr):
    assert T._same_company(home, adr) is True


@pytest.mark.parametrize("home,other", [
    # Both of these are live NYSE tickers a nesting match would have accepted, and both
    # are entirely different companies. This is the case worth protecting: substituting
    # the wrong company is far worse than dropping the right one.
    ("RELIANCE INDUSTRIES LIMITED", "RELIANCE INC"),
    ("TITAN CO LTD", "TITAN AMERICA SA"),
    # What a same-TICKER probe for JSE 'FSR' actually turns up in the US.
    ("FIRSTRAND LTD", "FISKER INC"),
    ("FIRSTRAND LTD", "FIRST RELIANCE BANCSHARES IN"),
    ("FIRSTRAND LTD", "FAST RADIUS INC"),
    ("HCL TECHNOLOGIES LTD", "HCL INFOSYSTEMS LTD"),
    ("INFOSYS LTD", "SLONE INFOSYSTEMS LTD"),
])
def test_a_different_company_is_never_taken_for_the_same_one(home, other):
    assert T._same_company(home, other) is False


def test_two_letters_agreeing_is_not_a_prefix_match():
    """The prefix rule needs three characters; below that it is coincidence."""
    assert T._token_match("TOB", "TOBACCO") is True
    assert T._token_match("TO", "TOBACCO") is False


# ── choosing among search results ───────────────────────────────────────────

_SBK_HITS = _ds(
    _C("SBK", currency="ZAR", primaryExchange="JSE", description="STANDARD BANK GROUP LTD"),
    _C("SKC2", currency="EUR", primaryExchange="FWB2", description="STANDARD BANK GROUP LTD"),
    _C("SGBLY", primaryExchange="PINK", description="STANDARD BANK GROUP-SPON ADR"),
)


def test_the_home_listing_and_other_foreign_lines_are_never_the_answer():
    """Only USD stock on a US venue qualifies — the ZAR and EUR lines are the problem."""
    assert T.pick_us_listing(_SBK_HITS, "STANDARD BANK GROUP LTD", allow_otc=False) is None


def test_otc_is_off_by_default_and_reachable_when_asked_for():
    assert T.pick_us_listing(_SBK_HITS, "STANDARD BANK GROUP LTD") is None
    assert T.pick_us_listing(_SBK_HITS, "STANDARD BANK GROUP LTD",
                             allow_otc=True) == ("SGBLY", "PINK")


def test_a_real_exchange_outranks_a_pink_quote_of_the_same_company():
    hits = _ds(_C("NGLOY", primaryExchange="PINK", description="ANGLO AMERICAN PLC-SPONS ADR"),
               _C("AA", primaryExchange="NYSE", description="ANGLO AMERICAN PLC-SPONS ADR"))
    assert T.pick_us_listing(hits, "ANGLO AMERICAN PLC", allow_otc=True) == ("AA", "NYSE")


def test_bonds_and_derivatives_in_the_search_results_are_ignored():
    hits = _ds(_C("", secType="BOND", description="FirstRand Ltd"),
               _C("FSRDQ", secType="STK", primaryExchange="VALUE", description="FAST RADIUS INC"))
    assert T.pick_us_listing(hits, "FIRSTRAND LTD", allow_otc=True) is None


# ── the policy over a universe ──────────────────────────────────────────────

_TABLE = [
    _C("FSR", currency="ZAR", primaryExchange="JSE", description="FIRSTRAND LTD"),
    _C("FSRNQ", primaryExchange="DOLLR4LOT", description="FISKER INC"),
    _C("FANDY", primaryExchange="PINK", description="FIRSTRAND LTD-UNSPON ADR"),
    _C("SOL", currency="ZAR", primaryExchange="JSE", description="SASOL LTD"),
    _C("SSL", primaryExchange="NYSE", description="SASOL LTD-SPONSORED ADR"),
    _C("INFY", currency="INR", primaryExchange="NSE", description="INFOSYS LTD"),
    _C("INFY", primaryExchange="NYSE", description="INFOSYS LTD-SP ADR"),
]


def test_a_name_with_a_us_listing_is_rerouted_to_it():
    ib = _IB(_TABLE)
    kept = T.enforce_stock_venue_policy([_stock("SOL", "SASOL LTD", "ZAR", "JSE")], ib)
    assert [(s.symbol, s.exchange, s.currency) for s in kept] == [("SSL", "SMART", "USD")]


def test_a_name_that_trades_only_at_home_is_dropped_not_kept():
    """FirstRand's only US quote is an unsponsored pink ADR, which this policy does not
    accept — so the name leaves the universe rather than occupying an unfillable slot."""
    ib = _IB(_TABLE)
    assert T.enforce_stock_venue_policy([_stock("FSR", "FIRSTRAND LTD", "ZAR", "JSE")], ib) == []


def test_tradable_names_are_passed_through_untouched_and_cost_no_lookups():
    ib = _IB(_TABLE)
    rows = [_stock("AAPL", "APPLE INC", "USD", "SMART"),
            _stock("WKL", "WOLTERS KLUWER", "EUR", "AEB"),
            _stock("TD", "TORONTO-DOMINION BANK", "CAD", "SMART")]
    kept = T.enforce_stock_venue_policy(rows, ib)
    assert [s.symbol for s in kept] == ["AAPL", "WKL", "TD"]
    assert [(s.exchange, s.currency) for s in kept] == [("SMART", "USD"), ("AEB", "EUR"), ("SMART", "CAD")]
    assert ib.patterns == []          # no broker round-trip for a name we can already trade


def test_the_us_line_is_not_added_twice_when_the_universe_already_holds_it():
    """Infosys is in the pool as NSE 'INFY' and would substitute to NYSE 'INFY'. One row."""
    ib = _IB(_TABLE)
    rows = [_stock("INFY", "INFOSYS LTD", "USD", "SMART"),
            _stock("INFY", "INFOSYS LTD", "INR", "NSE")]
    kept = T.enforce_stock_venue_policy(rows, ib)
    assert [(s.symbol, s.currency) for s in kept] == [("INFY", "USD")]


def test_a_broker_failure_drops_the_name_rather_than_keeping_it():
    """Fail-closed: an unanswered lookup must not leave an untradable name in the roster."""
    ib = _IB(_TABLE, boom=True)
    assert T.enforce_stock_venue_policy([_stock("SOL", "SASOL LTD", "ZAR", "JSE")], ib) == []


def test_the_blocked_set_is_the_only_thing_that_selects_a_name():
    """Nothing here is JSE-specific — the same machinery covers any venue we lose."""
    ib = _IB(_TABLE)
    rows = [_stock("SOL", "SASOL LTD", "ZAR", "JSE")]
    assert T.enforce_stock_venue_policy(rows, ib, blocked=set()) == rows
    assert {"ZAR", "INR"} <= T.UNTRADABLE_STOCK_CURRENCIES


def test_the_caller_must_rebind_because_dropping_returns_a_new_list():
    ib = _IB(_TABLE)
    rows = [_stock("FSR", "FIRSTRAND LTD", "ZAR", "JSE"), _stock("AAPL", "APPLE INC", "USD", "SMART")]
    kept = T.enforce_stock_venue_policy(rows, ib)
    assert len(rows) == 2 and len(kept) == 1


# ── the live buyer half of the same rule ────────────────────────────────────
#
# The screen runs monthly, so it cannot be the only gate: a blocked name already in the
# watchlist (FSR and SBK are, today) has to be kept out of every scan in between. Both
# sides read one list, src.portfolio.venues.

from types import SimpleNamespace

from src.portfolio.venues import (UNTRADABLE_STOCK_CURRENCIES as LIVE_BLOCKED,
                                  is_untradable_currency, partition_tradable)


def _row(symbol, currency, exchange):
    return SimpleNamespace(symbol=symbol, currency=currency, exchange=exchange)


def test_the_screener_and_the_buyer_read_the_same_list():
    """Two enforcement points, one rule — they must not be able to drift apart."""
    assert T.UNTRADABLE_STOCK_CURRENCIES is LIVE_BLOCKED


@pytest.mark.parametrize("ccy,blocked", [
    ("ZAR", True), ("INR", True), ("zar", True), (" inr ", True),
    ("USD", False), ("EUR", False), ("JPY", False), ("HKD", False), ("AUD", False),
])
def test_only_the_two_unpermissioned_venues_are_blocked(ccy, blocked):
    """HKD/AUD/JPY are explicitly NOT blocked: their orders fail on board-lot size, not
    permissions, and blocking them would strand names that can actually be bought."""
    assert is_untradable_currency(ccy) is blocked


def test_a_row_with_no_currency_is_left_to_the_callers_other_gates():
    """Defaulting an unknown currency to 'blocked' would empty the buy universe silently."""
    assert is_untradable_currency(None) is False
    assert is_untradable_currency("") is False


def test_blocked_names_leave_the_buy_universe_and_order_is_preserved():
    rows = [_row("AAPL", "USD", "SMART"), _row("FSR", "ZAR", "JSE"),
            _row("WKL", "EUR", "AEB"), _row("INFY", "INR", "NSE"),
            _row("TD", "CAD", "SMART")]
    tradable, blocked = partition_tradable(rows)
    assert [r.symbol for r in tradable] == ["AAPL", "WKL", "TD"]
    assert [r.symbol for r in blocked] == ["FSR", "INFY"]


def test_nothing_is_blocked_when_the_watchlist_is_all_tradable():
    rows = [_row("AAPL", "USD", "SMART"), _row("6920", "JPY", "TSEJ"),
            _row("2318", "HKD", "SEHK"), _row("XRO", "AUD", "ASX")]
    tradable, blocked = partition_tradable(rows)
    assert tradable == rows and blocked == []


def test_an_empty_or_missing_watchlist_does_not_raise():
    assert partition_tradable([]) == ([], [])
    assert partition_tradable(None) == ([], [])
