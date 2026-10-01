"""Monthly holdings review — the SUGGESTION maker (it never sells; every card needs approval).

Growth tier: a sell / covered-call card only for a REAL drop-off, defined by the same growth
gate the screener uses for entry — durable growth below the floor AND the latest half-year
confirming it. The old test was "trailing growth < 15%", which described about half the tier.

Also pinned here: FMP is keyed by US ticker, so a non-USD holding is only judged on FMP data
when FMP is talking about the same company (Verbund's ticker VER returns VEREIT, a US REIT
with a dividend cut); and the dividend tier's shrinking-revenue test, dead until 2026-10-01
because it read a key that was never written, is a corroborating signal, not a primary one.
"""
import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base
import src.core.database as database_mod
import src.core.suggestions as suggestions_mod
import src.portfolio.analyzer as analyzer_mod
import src.portfolio.connection as connection_mod
import src.portfolio.fmp as fmp_mod
import src.portfolio.scheduler as sched
from src.portfolio.models import PortfolioHolding, PortfolioTransaction

PRICE = 100.0


@pytest.fixture
def review(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()

    @contextmanager
    def fake_get_db():
        yield sess

    cards, trends, fundamentals, profiles = [], {}, {}, {}
    state = SimpleNamespace(sma=100.0)

    monkeypatch.setattr(database_mod, "get_db", fake_get_db)
    monkeypatch.setattr(suggestions_mod, "create_suggestion", lambda **kw: cards.append(kw))
    monkeypatch.setattr(connection_mod, "get_portfolio_stock_price", lambda *a, **k: PRICE)
    monkeypatch.setattr(analyzer_mod, "PortfolioAnalyzer", lambda ib: SimpleNamespace(
        analyze_stock=lambda *a, **k: SimpleNamespace(sma_200=state.sma)))
    monkeypatch.setattr(fmp_mod, "get_growth_trend", lambda s: trends.get(s))
    monkeypatch.setattr(fmp_mod, "get_full_fundamentals", lambda s: fundamentals.get(s))
    monkeypatch.setattr(fmp_mod, "_get", lambda endpoint, s, params={}: profiles.get(s))
    monkeypatch.setattr(sched, "_get_chronos_trend", lambda s: None)

    def hold(symbol, tier="growth", shares=50, pnl_pct=0.0, held_days=200, currency="USD",
             name=None, value=10_000.0):
        sess.add(PortfolioHolding(
            symbol=symbol, name=name or symbol, tier=tier, shares=shares, avg_cost=PRICE,
            current_price=PRICE, market_value=value, total_invested=value,
            unrealized_pnl_pct=pnl_pct, currency=currency, exchange="SMART"))
        sess.add(PortfolioTransaction(
            symbol=symbol, action="buy", shares=shares, price=PRICE, amount=value,
            created_at=dt.datetime.utcnow() - dt.timedelta(days=held_days)))
        sess.flush()

    def run():
        symbols = {h.symbol for h in sess.query(PortfolioHolding).all()}
        tiers = {h.symbol: h.tier for h in sess.query(PortfolioHolding).all()}
        cfg = SimpleNamespace(cash_yield_symbol="XEON")
        sched._review_existing_holdings_monthly(None, cfg, symbols, tiers)
        return cards

    # Enough ballast that no single test holding trips the 12% concentration rule.
    for i in range(12):
        hold(f"PAD{i}", tier="breakthrough")
    return SimpleNamespace(hold=hold, run=run, trends=trends, fundamentals=fundamentals,
                           profiles=profiles, state=state)


def _trend(cagr, ttm, half):
    return {"revenue_cagr_pct": cagr, "revenue_ttm_pct": ttm, "revenue_recent_half_pct": half}


def _cards_for(cards, symbol):
    return [(c["action"], c["signal"]) for c in cards if c["symbol"] == symbol]


# ── growth tier ──────────────────────────────────────────────────────────────

def test_real_dropoff_held_long_gets_a_sell_card_with_the_evidence(review):
    review.hold("MKTX")
    review.trends["MKTX"] = _trend(4.9, 3.4, 2.0)
    review.fundamentals["MKTX"] = {"dividend_yield": 1.6}
    cards = review.run()
    assert _cards_for(cards, "MKTX") == [("sell_stock_review", "monthly_growth_thesis_weak")]
    rationale = cards[0]["rationale"]
    assert "real growth drop-off" in rationale and "floor 8%" in rationale


def test_healthy_grower_under_the_old_15pct_line_gets_no_card(review):
    # KLAC: 10%/yr, 12% trailing, 13% latest half. The old rule (< 15%) carded it.
    review.hold("KLAC")
    review.trends["KLAC"] = _trend(10.2, 11.7, 13.4)
    review.fundamentals["KLAC"] = {"dividend_yield": 0.7}
    assert _cards_for(review.run(), "KLAC") == []


def test_reaccelerating_name_below_the_floor_gets_no_card(review):
    # EW: fails the entry floor on history (frozen by the screen) but is growing 15% now.
    review.hold("EW")
    review.trends["EW"] = _trend(3.8, 14.6, 15.1)
    review.fundamentals["EW"] = {"dividend_yield": 0.0}
    assert _cards_for(review.run(), "EW") == []


def test_dropoff_bought_recently_gets_no_sell_card(review):
    review.hold("UNP", held_days=20)
    review.trends["UNP"] = _trend(3.0, 4.2, 7.4)
    review.fundamentals["UNP"] = {"dividend_yield": 2.2}
    assert _cards_for(review.run(), "UNP") == []


def test_dropoff_well_above_trend_and_in_profit_also_gets_a_call_card(review):
    review.state.sma = 80.0                                   # price 25% above the 200d average
    review.hold("LULU", shares=200, pnl_pct=30.0)
    review.trends["LULU"] = _trend(15.4, 1.7, 1.0)
    review.fundamentals["LULU"] = {"dividend_yield": 0.0}
    got = _cards_for(review.run(), "LULU")
    assert ("sell_stock_review", "monthly_growth_thesis_weak") in got
    assert ("sell_covered_call_review", "monthly_cc_review") in got


def test_grower_well_above_trend_gets_no_call_card(review):
    review.state.sma = 80.0
    review.hold("MA", shares=200, pnl_pct=30.0)
    review.trends["MA"] = _trend(14.8, 16.0, 15.0)
    review.fundamentals["MA"] = {"dividend_yield": 0.5}
    assert _cards_for(review.run(), "MA") == []


def test_no_revenue_history_means_no_card(review):
    review.hold("XRO", currency="AUD", name="XERO LTD")
    review.fundamentals["XRO"] = {"dividend_yield": 0.0}
    assert _cards_for(review.run(), "XRO") == []


def test_dropoff_with_unknown_latest_half_gets_no_card(review):
    review.hold("OLD")
    review.trends["OLD"] = _trend(2.0, 1.0, None)
    review.fundamentals["OLD"] = {"dividend_yield": 0.0}
    assert _cards_for(review.run(), "OLD") == []


def test_breakthrough_is_never_carded_on_growth(review):
    review.hold("RKLB", tier="breakthrough")
    review.trends["RKLB"] = _trend(-5.0, -10.0, -12.0)
    assert _cards_for(review.run(), "RKLB") == []


# ── FMP identity ─────────────────────────────────────────────────────────────

DIV_CUT = {"dividend_cut": True, "payout_ratio": 0, "revenue_yoy_pct": -6.1,
           "revenue_avg_annual_pct": -2.4, "fcf_negative_years": 1, "debt_to_equity": 0.9}


def test_foreign_holding_is_not_judged_on_another_companys_data(review):
    review.hold("VER", tier="dividend", currency="EUR", name="VERBUND AG")
    review.profiles["VER"] = [{"companyName": "VEREIT, Inc."}]
    review.fundamentals["VER"] = DIV_CUT
    assert _cards_for(review.run(), "VER") == []


def test_foreign_holding_with_matching_us_line_is_judged(review):
    review.hold("SU", tier="dividend", currency="CAD", name="SUNCOR ENERGY INC")
    review.profiles["SU"] = [{"companyName": "Suncor Energy Inc."}]
    review.fundamentals["SU"] = DIV_CUT
    assert _cards_for(review.run(), "SU") == [("sell_stock_review", "monthly_dividend_disqualified")]


def test_identity_check_fails_closed_without_a_profile(review):
    assert sched._fmp_matches_holding("ING", "ING GROEP NV", "EUR") is False
    assert sched._fmp_matches_holding("AAPL", "APPLE INC", "USD") is True


# ── dividend tier: shrinking revenue is corroboration, not proof ─────────────

def test_shrinking_revenue_alone_does_not_card_a_dividend_payer(review):
    review.hold("SU", tier="dividend")
    review.fundamentals["SU"] = {"dividend_cut": False, "payout_ratio": 40,
                                 "revenue_yoy_pct": -3.5, "revenue_avg_annual_pct": -5.4,
                                 "fcf_negative_years": 0, "debt_to_equity": 0.4}
    assert _cards_for(review.run(), "SU") == []


def test_shrinking_revenue_plus_negative_cash_flow_cards_a_dividend_payer(review):
    review.hold("WEAK", tier="dividend")
    review.fundamentals["WEAK"] = {"dividend_cut": False, "payout_ratio": 40,
                                   "revenue_yoy_pct": -3.5, "revenue_avg_annual_pct": -5.4,
                                   "fcf_negative_years": 2, "debt_to_equity": 0.4}
    cards = review.run()
    assert _cards_for(cards, "WEAK") == [("sell_stock_review", "monthly_dividend_disqualified")]
    assert "revenue shrinking" in cards[0]["rationale"]
