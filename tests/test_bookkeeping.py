"""Bookkeeping bridge: translation correctness on the sample Flex statement.

Run:  PYTHONPATH=. .venv/bin/python -m pytest tests/test_bookkeeping.py -q
"""
from pathlib import Path

import pytest

from src.bookkeeping.config import BookkeepingConfig, FlexConn, SBConn
from src.bookkeeping.daily_sync import run_daily_sync
from src.bookkeeping.flex_extract import parse_flex
from src.bookkeeping.journal import translate_day

SAMPLE = Path("src/bookkeeping/sample_flex.xml")


@pytest.fixture
def cfg() -> BookkeepingConfig:
    return BookkeepingConfig(
        enabled=True, dry_run=True, base_currency="EUR",
        flex=FlexConn(token="x", query_id="x"),
        standard_books=SBConn(base_url="http://sb.local", company="1",
                              username="api", password="secret"),
        accounts={
            "securities": "1810", "commission": "5610", "realized_pnl": "6500",
            "dividend_income": "6100", "withholding_tax": "1760",
            "interest_income": "6200", "interest_expense": "5620",
            "fees": "5600", "fx_gain_loss": "6300", "equity": "2510",
            "cash": {"USD": "1910", "EUR": "1900"},
        },
    )


def _entries(cfg):
    return translate_day(parse_flex(SAMPLE.read_text()), cfg)


def test_every_journal_balances(cfg):
    entries = _entries(cfg)
    assert len(entries) == 8
    for je in entries:
        assert je.is_balanced(), f"{je.reference} imbalance={je.imbalance}"


def test_sell_books_gain_as_credit(cfg):
    sell = next(e for e in _entries(cfg) if e.reference == "IBKR:102")
    pnl = next(r for r in sell.rows if r.account == "6500")
    assert pnl.credit == pytest.approx(2300.00)   # gross gain (proceeds-cost) in EUR
    assert pnl.debit == 0
    sec = next(r for r in sell.rows if r.account == "1810")
    assert sec.credit == pytest.approx(18400.00)   # cost basis removed, not proceeds


def test_withholding_tax_debits_tax_credits_cash(cfg):
    wht = next(e for e in _entries(cfg) if e.reference == "IBKR:202")
    assert next(r for r in wht.rows if r.account == "1760").debit == pytest.approx(3.45)
    assert next(r for r in wht.rows if r.account == "1910").credit == pytest.approx(3.45)


def test_fx_base_leg_not_reconverted(cfg):
    fx = next(e for e in _entries(cfg) if e.reference == "IBKR:103")
    eur_leg = next(r for r in fx.rows if r.account == "1900")
    assert eur_leg.debit == pytest.approx(10000.00)   # EUR leg kept at face, plug ~spread only
    plug = next(r for r in fx.rows if r.account == "6300")
    assert plug.debit + plug.credit < 1.0


def test_dry_run_never_posts(cfg):
    report = run_daily_sync(dry_run=True, flex_xml=SAMPLE.read_text(),
                            config=cfg, print_journals=False)
    assert report.posted == 0 and report.failed == 0
    assert all(r.dry_run for r in report.results)
