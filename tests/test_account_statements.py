"""
Flex archive + account statement PDF.

Covers the two things that make or break this feature: idempotent re-import
(the same statement fetched twice must not double the books) and as-of holdings
resolving to the right snapshot date.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.reports import account_statement as stmt
from src.reports import flex_archive as fa


FLEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="TaxReport" type="AF">
 <FlexStatements count="1">
  <FlexStatement accountId="U9999999" fromDate="20250101" toDate="20251231"
                 period="Year" whenGenerated="20260102;120000">
   <Trades>
     <Trade transactionID="1001" tradeDate="20250310" symbol="AAPL"
            description="APPLE INC" assetCategory="STK" buySell="BUY"
            quantity="100" tradePrice="180.5" proceeds="-18050" ibCommission="-1.05"
            netCash="-18051.05" cost="18051.05" fifoPnlRealized="0"
            currency="USD" fxRateToBase="0.92" levelOfDetail="EXECUTION"/>
     <Trade transactionID="1002" tradeDate="20250715" symbol="AAPL"
            description="APPLE INC" assetCategory="STK" buySell="SELL"
            quantity="-40" tradePrice="210" proceeds="8400" ibCommission="-1.02"
            netCash="8398.98" cost="7220.42" fifoPnlRealized="1178.56"
            currency="USD" fxRateToBase="0.86" levelOfDetail="EXECUTION"/>
     <Trade transactionID="9001" tradeDate="20250715" symbol="AAPL"
            assetCategory="STK" buySell="SELL" quantity="-40" tradePrice="210"
            proceeds="8400" ibCommission="-1.02" netCash="8398.98"
            currency="USD" fxRateToBase="0.86" levelOfDetail="ORDER"/>
   </Trades>
   <CashTransactions>
     <CashTransaction transactionID="2001" settleDate="20250515" type="Dividends"
                      symbol="AAPL" description="AAPL CASH DIVIDEND USD 0.25"
                      amount="25" currency="USD" fxRateToBase="0.88"/>
     <CashTransaction transactionID="2002" settleDate="20250515"
                      type="Withholding Tax" symbol="AAPL"
                      description="AAPL WITHHOLDING @ 15%" amount="-3.75"
                      currency="USD" fxRateToBase="0.88"/>
     <CashTransaction transactionID="2003" settleDate="20250630"
                      type="Broker Interest Paid" description="USD DEBIT INT"
                      amount="-12.4" currency="USD" fxRateToBase="0.87"/>
   </CashTransactions>
   <DepositWithdrawals>
     <DepositWithdrawal transactionID="3001" settleDate="20250201"
                        description="WIRE IN" amount="50000" currency="EUR"
                        fxRateToBase="1.0"/>
   </DepositWithdrawals>
   <OpenPositions>
     <OpenPosition reportDate="20251231" symbol="AAPL" description="APPLE INC"
                   assetCategory="STK" conid="265598" position="60"
                   multiplier="1" markPrice="250" positionValue="15000"
                   costBasisPrice="180.51" costBasisMoney="10830.63"
                   fifoPnlUnrealized="4169.37" currency="USD" fxRateToBase="0.9"
                   levelOfDetail="SUMMARY"/>
     <OpenPosition reportDate="20251231" symbol="AAPL" description="APPLE INC"
                   assetCategory="STK" position="60" markPrice="250"
                   positionValue="15000" currency="USD" fxRateToBase="0.9"
                   levelOfDetail="LOT"/>
   </OpenPositions>
   <EquitySummaryInBase>
     <EquitySummaryByReportDateInBase reportDate="20251231" currency="EUR"
                                      cash="12000" stock="13500" options="0"
                                      total="25500"/>
   </EquitySummaryInBase>
  </FlexStatement>
 </FlexStatements>
</FlexQueryResponse>
"""


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the app at a throwaway DB + archive dir for each test."""
    from src.core import config as cfg_mod
    from src.core import database as db_mod

    cfg_mod.get_settings.cache_clear()
    settings = cfg_mod.get_settings()
    monkeypatch.setattr(settings.app, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings.app, "options_db_path", str(tmp_path / "o.db"))
    monkeypatch.setattr(settings.ibkr, "account", "U9999999")

    db_mod._engine = None
    db_mod._options_engine = None
    db_mod._SessionLocal = None
    db_mod.init_db()

    monkeypatch.setattr(fa, "ARCHIVE_DIR", tmp_path / "flex_archive")
    monkeypatch.setattr(stmt, "STATEMENTS_DIR", tmp_path / "statements")
    yield
    db_mod._engine = None
    db_mod._options_engine = None
    db_mod._SessionLocal = None


# ── parsing ──────────────────────────────────────────────────────────────

def test_parse_pulls_every_section():
    (p,) = fa.parse_statement(FLEX_XML)
    assert p.account_id == "U9999999"
    assert (p.from_date, p.to_date) == ("2025-01-01", "2025-12-31")

    kinds = [e.kind for e in p.events]
    assert kinds.count("trade") == 2        # ORDER-level duplicate dropped
    assert kinds.count("cash") == 3
    assert kinds.count("deposit") == 1
    assert len(p.positions) == 1            # LOT duplicate dropped
    assert p.positions[0].level_of_detail == "SUMMARY"
    assert p.equity[0].total == 25500


def test_order_level_trades_dropped_only_when_execution_present():
    """A query configured order-level-only must still yield its trades."""
    xml = FLEX_XML.replace('levelOfDetail="EXECUTION"', 'levelOfDetail="ORDER"')
    (p,) = fa.parse_statement(xml)
    assert len([e for e in p.events if e.kind == "trade"]) == 3


def test_categorization_splits_dividend_from_withholding():
    (p,) = fa.parse_statement(FLEX_XML)
    cats = {e.symbol + "/" + e.event_type: stmt._categorize(e.kind, e.event_type)
            for e in p.events if e.kind == "cash"}
    assert cats["AAPL/Dividends"] == "Dividends"
    assert cats["AAPL/Withholding Tax"] == "Withholding tax"
    assert cats["/Broker Interest Paid"] == "Interest"


def test_rejects_non_flex_xml():
    with pytest.raises(ValueError, match="FlexStatement"):
        fa.parse_statement("<FlexQueryResponse><Status>Fail</Status></FlexQueryResponse>")


# ── archive ──────────────────────────────────────────────────────────────

def test_import_is_idempotent():
    first = fa.import_from_xml("options", FLEX_XML)
    assert first["events_added"] == 6        # 2 trades + 3 cash + 1 deposit
    assert first["positions"] == 1
    assert not first["already_imported"]

    second = fa.import_from_xml("options", FLEX_XML)
    assert second["events_added"] == 0       # nothing new
    assert second["events_updated"] == 6
    assert second["already_imported"]

    status = fa.archive_status("options")
    assert status["n_events"] == 6
    assert status["n_statements"] == 1
    assert status["position_dates"] == ["2025-12-31"]


def test_reimport_does_not_stack_position_snapshots():
    fa.import_from_xml("options", FLEX_XML)
    fa.import_from_xml("options", FLEX_XML)
    from src.core.database import get_db
    from src.reports.models import FlexPositionSnapshot
    with get_db() as db:
        n = db.query(FlexPositionSnapshot).filter(
            FlexPositionSnapshot.report_date == "2025-12-31").count()
    assert n == 1


def test_sides_are_isolated():
    fa.import_from_xml("options", FLEX_XML)
    assert fa.archive_status("options")["n_events"] == 6
    assert fa.archive_status("portfolio")["n_events"] == 0


def test_wrong_account_import_is_refused():
    """Two accounts, two companies. A statement imported on the wrong tab would
    merge one set of books into the other — refuse it instead."""
    with pytest.raises(ValueError, match="U9999999"):
        fa.import_from_xml("portfolio", FLEX_XML)   # portfolio is U26413485
    assert fa.archive_status("portfolio")["n_events"] == 0


def test_rejects_unknown_side():
    with pytest.raises(ValueError, match="side must be one of"):
        fa.import_from_xml("nonsense", FLEX_XML)


def test_earliest_activity():
    fa.import_from_xml("options", FLEX_XML)
    assert fa.earliest_activity("options") == date(2025, 2, 1)


# ── statement assembly ───────────────────────────────────────────────────

def test_collect_range_and_totals():
    fa.import_from_xml("options", FLEX_XML)
    data = stmt.collect("options", date(2025, 1, 1), date(2025, 12, 31),
                        [date(2025, 12, 31)])

    assert len(data.transactions) == 6
    assert data.coverage_gaps == []

    by_cat = {t.category: t for t in data.totals}
    assert by_cat["Dividends"].amount == pytest.approx(25.0)
    assert by_cat["Withholding tax"].amount == pytest.approx(-3.75)
    assert by_cat["Interest"].amount == pytest.approx(-12.4)
    assert by_cat["Trades"].realized_pnl == pytest.approx(1178.56)
    assert by_cat["Deposits & withdrawals"].currency == "EUR"


def test_range_filter_excludes_outside_events():
    fa.import_from_xml("options", FLEX_XML)
    data = stmt.collect("options", date(2025, 6, 1), date(2025, 12, 31),
                        [date(2025, 12, 31)])
    dates = {t.event_date for t in data.transactions}
    assert dates == {"2025-06-30", "2025-07-15"}


def test_holdings_exact_and_fallback():
    fa.import_from_xml("options", FLEX_XML)
    data = stmt.collect("options", date(2025, 1, 1), date(2025, 12, 31),
                        [date(2025, 12, 31), date(2026, 3, 1), date(2024, 1, 1)])
    exact, later, earlier = data.holdings

    assert exact.is_exact and len(exact.rows) == 1
    assert exact.rows[0].quantity == 60
    assert exact.equity["total"] == 25500

    # A date after the last snapshot resolves backwards and says so.
    assert later.actual_as_of == date(2025, 12, 31)
    assert not later.is_exact and "nearest earlier" in later.note

    # A date before any snapshot has no answer at all — reported, not faked.
    assert earlier.actual_as_of is None
    assert "No open-position snapshot" in earlier.note


def test_coverage_gaps_are_reported():
    fa.import_from_xml("options", FLEX_XML)
    data = stmt.collect("options", date(2024, 1, 1), date(2026, 6, 30),
                        [date(2025, 12, 31)])
    assert (date(2024, 1, 1), date(2024, 12, 31)) in data.coverage_gaps
    assert (date(2026, 1, 1), date(2026, 6, 30)) in data.coverage_gaps
    assert data.warnings


def test_empty_archive_reports_whole_range_as_gap():
    data = stmt.collect("portfolio", date(2025, 1, 1), date(2025, 12, 31), [])
    assert data.transactions == []
    assert data.coverage_gaps == [(date(2025, 1, 1), date(2025, 12, 31))]


def test_collect_rejects_backwards_range():
    with pytest.raises(ValueError, match="before start"):
        stmt.collect("options", date(2025, 12, 31), date(2025, 1, 1), [])


# ── presets + PDF ────────────────────────────────────────────────────────

def test_presets_shape():
    presets = {p["key"]: p for p in stmt.period_presets(date(2026, 8, 2))}
    ly = presets["last_year"]
    assert (ly["start"], ly["end"]) == (date(2025, 1, 1), date(2025, 12, 31))
    assert ly["as_of"] == [date(2024, 12, 31), date(2025, 12, 31)]

    ty = presets["this_year"]
    assert (ty["start"], ty["end"]) == (date(2026, 1, 1), date(2026, 8, 2))
    assert ty["as_of"] == [date(2025, 12, 31), date(2026, 8, 2)]

    assert presets["all_time"]["start"] is None


def test_all_time_preset_resolves_to_first_activity():
    fa.import_from_xml("options", FLEX_XML)
    p = stmt.resolve_preset("options", "all_time", date(2026, 8, 2))
    assert p["start"] == date(2025, 2, 1)
    assert p["end"] == date(2026, 8, 2)


def test_generate_writes_a_pdf():
    fa.import_from_xml("options", FLEX_XML)
    path, data = stmt.generate("options", date(2025, 1, 1), date(2025, 12, 31),
                               [date(2025, 12, 31), date(2026, 8, 2)])
    assert path.exists()
    assert path.read_bytes()[:5] == b"%PDF-"
    assert path.stat().st_size > 2000
    assert len(data.holdings) == 2
    # STATEMENTS_DIR must resolve at call time, or a test run writes into data/.
    assert path.is_relative_to(stmt.STATEMENTS_DIR)


def test_generate_pdf_on_empty_archive_still_renders():
    """A statement for a period with nothing in it must still produce a
    readable document saying so, not blow up."""
    path, data = stmt.generate("portfolio", date(2025, 1, 1), date(2025, 12, 31),
                               [date(2025, 12, 31)])
    assert path.exists()
    assert path.read_bytes()[:5] == b"%PDF-"
    assert data.coverage_gaps
