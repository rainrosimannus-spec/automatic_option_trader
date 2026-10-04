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

    cards, trends, fundamentals, profiles, broker = [], {}, {}, {}, {}
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
    # The broker's own figures for a holding (src/portfolio/ibkr_fundamentals). {} = broker has nothing.
    monkeypatch.setattr(sched, "_broker_review_figures",
                        lambda ib, holding, symbol, price: broker.get(symbol, {}))

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
                           profiles=profiles, state=state, broker=broker, sess=sess)


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


# ── broker figures: holdings the data provider cannot see ────────────────────

def _btrend(cagr, ttm, half, **more):
    return {"revenue_cagr_pct": cagr, "revenue_ttm_pct": ttm, "revenue_recent_half_pct": half, **more}


def test_foreign_growth_dropoff_is_carded_from_broker_figures(review):
    # No provider data at all for this listing (no profile -> identity check fails closed).
    review.hold("XRO", currency="AUD", name="XERO LTD")
    review.broker["XRO"] = _btrend(4.0, 2.0, 1.0)
    cards = review.run()
    assert _cards_for(cards, "XRO") == [("sell_stock_review", "monthly_growth_thesis_weak")]
    assert "figures: broker" in cards[0]["rationale"] and "floor 8%" in cards[0]["rationale"]


def test_foreign_grower_gets_no_card_from_broker_figures(review):
    review.hold("XRO", currency="AUD", name="XERO LTD")
    review.broker["XRO"] = _btrend(25.1, 17.1, 13.8)            # Xero's real figures
    assert _cards_for(review.run(), "XRO") == []


def test_provider_figures_are_used_when_they_describe_this_company(review):
    # ASML: Amsterdam holding, provider has its US line under the same name.
    review.hold("ASML", currency="EUR", name="ASML HOLDING NV")
    review.profiles["ASML"] = [{"companyName": "ASML Holding N.V."}]
    review.trends["ASML"] = _trend(15.0, 12.0, 11.0)
    review.fundamentals["ASML"] = {"dividend_yield": 0.9}
    review.broker["ASML"] = _btrend(1.0, 1.0, 1.0)             # would be a drop-off if it were used
    assert _cards_for(review.run(), "ASML") == []


def test_broker_dropoff_without_a_latest_half_year_gets_no_card(review):
    review.hold("PRX", currency="EUR", name="PROSUS NV")
    review.broker["PRX"] = _btrend(2.0, 1.0, None)
    assert _cards_for(review.run(), "PRX") == []


def test_foreign_dividend_cut_is_carded_from_broker_figures(review):
    review.hold("INGA", tier="dividend", currency="EUR", name="ING GROEP NV")
    review.broker["INGA"] = {"dividend_cut": True, "payout_ratio": 45.0}
    cards = review.run()
    assert _cards_for(cards, "INGA") == [("sell_stock_review", "monthly_dividend_disqualified")]
    assert "dividend cut detected" in cards[0]["rationale"] and "figures: broker" in cards[0]["rationale"]


def test_healthy_foreign_dividend_payer_gets_no_card(review):
    review.hold("WKL", tier="dividend", currency="EUR", name="WOLTERS KLUWER")
    review.broker["WKL"] = {"dividend_cut": False, "payout_ratio": 48.0, "revenue_yoy_pct": 6.0,
                            "revenue_cagr_pct": 5.0, "fcf_negative_years_5yr": 0, "net_debt_to_equity": 1.4}
    assert _cards_for(review.run(), "WKL") == []


def test_foreign_payer_with_two_secondary_weaknesses_is_carded(review):
    review.hold("WEAKEU", tier="dividend", currency="EUR", name="WEAK NV")
    review.broker["WEAKEU"] = {"dividend_cut": False, "payout_ratio": 60.0, "revenue_yoy_pct": -4.0,
                               "revenue_cagr_pct": -3.0, "fcf_negative_years_5yr": 3, "net_debt_to_equity": 0.5}
    cards = review.run()
    assert _cards_for(cards, "WEAKEU") == [("sell_stock_review", "monthly_dividend_disqualified")]
    assert "revenue shrinking" in cards[0]["rationale"] and "FCF negative 3 years" in cards[0]["rationale"]


def test_broker_dividend_record_overrides_the_providers_yield_based_cut(review):
    # The provider calls it a "cut" when the YIELD falls 30%, which a rising price also does.
    # The broker reads dividends per share. Same precedence as the screen.
    review.hold("PEP", tier="dividend")
    review.fundamentals["PEP"] = {"dividend_cut": True, "payout_ratio": 0, "fcf_negative_years": 0,
                                  "debt_to_equity": 1.0, "revenue_yoy_pct": 3.0, "revenue_avg_annual_pct": 3.0}
    review.broker["PEP"] = {"dividend_cut": False, "payout_ratio": 70.0}
    assert _cards_for(review.run(), "PEP") == []


def test_verbund_is_judged_on_its_own_broker_figures_not_vereits(review):
    review.hold("VER", tier="dividend", currency="EUR", name="VERBUND AG")
    review.profiles["VER"] = [{"companyName": "VEREIT, Inc."}]
    review.fundamentals["VER"] = DIV_CUT                        # VEREIT's record: ignored
    review.broker["VER"] = {"dividend_cut": False, "payout_ratio": 55.0}
    assert _cards_for(review.run(), "VER") == []
    review.broker["VER"] = {"dividend_cut": True, "payout_ratio": 55.0}   # Verbund itself cuts
    assert ("sell_stock_review", "monthly_dividend_disqualified") in _cards_for(review.run(), "VER")


def test_nothing_from_either_source_means_no_card(review):
    review.hold("2318", tier="dividend", currency="HKD", name="PING AN INSURANCE GROUP CO-H")
    assert _cards_for(review.run(), "2318") == []


# ── quality drop-outs: the other pillar ──────────────────────────────────────

SUBSTANCE = ("return on capital 3.8% over five years, below the 8% it takes to cover the cost of "
             "that capital; net loss in 2 of the last five years")


def _reason(quality, substance=""):
    return ("No longer in screened universe — growth gate: quality "
            f"{quality} below the 50 floor" + (f" [in substance: {substance}]" if substance else "")
            + ". Pending removal — open position exists.")


def _dropped(review, symbol, reason, **hold_kw):
    from src.portfolio.models import PortfolioWatchlist
    review.hold(symbol, **hold_kw)
    review.sess.add(PortfolioWatchlist(symbol=symbol, name=symbol, tier="growth",
                                       currency=hold_kw.get("currency", "USD"),
                                       pending_removal=True, pending_removal_reason=reason))
    review.sess.flush()


def test_quality_fallen_below_the_sell_threshold_gets_a_card_that_says_why(review):
    _dropped(review, "XRO", _reason("38.4", SUBSTANCE), currency="AUD", name="XERO LTD", held_days=60)
    review.broker["XRO"] = _btrend(25.1, 17.1, 13.8)            # growth is fine
    cards = review.run()
    assert _cards_for(cards, "XRO") == [("sell_stock_review", "monthly_quality_dropout")]
    text = cards[0]["rationale"]
    assert "quality has fallen to 38.4" in text and "sell threshold of 40" in text
    assert "return on capital 3.8%" in text and "net loss in 2 of the last five years" in text
    assert "Growth is not the problem" in text and "frozen" in text


def test_sell_threshold_is_ten_points_under_the_entry_floor(review):
    _dropped(review, "AT40", _reason("40.0", SUBSTANCE))
    _dropped(review, "AT41", _reason("40.1", SUBSTANCE))
    cards = review.run()
    assert _cards_for(cards, "AT40") == [("sell_stock_review", "monthly_quality_dropout")]
    assert _cards_for(cards, "AT41") == []


def test_score_that_only_slipped_under_the_floor_is_frozen_not_carded(review):
    # 47: fails the gate, leaves the tier, gets no new money — and no sell card, whatever the facts.
    _dropped(review, "EDGE", _reason("47.0", SUBSTANCE))
    assert _cards_for(review.run(), "EDGE") == []


def test_low_score_is_carded_even_without_a_single_standout_failure(review):
    _dropped(review, "LOWALL", _reason("36.0"))
    cards = review.run()
    assert _cards_for(cards, "LOWALL") == [("sell_stock_review", "monthly_quality_dropout")]
    assert "In substance" not in cards[0]["rationale"]


def test_quality_card_does_not_wait_for_the_holding_period(review):
    _dropped(review, "MEIT", _reason("18.7", "net loss in 3 of the last five years"), held_days=5)
    assert _cards_for(review.run(), "MEIT") == [("sell_stock_review", "monthly_quality_dropout")]


def test_unmeasured_quality_gets_no_card(review):
    _dropped(review, "NODATA", "No longer in screened universe — growth gate: quality unmeasured "
                               "(no return-on-capital or operating-margin data). Pending removal — open position exists.")
    assert _cards_for(review.run(), "NODATA") == []


def test_being_outranked_or_dropped_for_growth_is_not_a_quality_card(review):
    # "Out of the selected 60" on its own is never a reason to sell.
    _dropped(review, "RANK61", "No longer in screened universe. Pending removal — open position exists.")
    _dropped(review, "SLOW", "No longer in screened universe — growth gate: durable growth 4.2% < 8% floor. "
                             "Pending removal — open position exists.", held_days=20)
    cards = review.run()
    assert _cards_for(cards, "RANK61") == [] and _cards_for(cards, "SLOW") == []


def test_current_member_is_never_carded_on_a_stale_reason(review):
    from src.portfolio.models import PortfolioWatchlist
    review.hold("MEMBER")
    review.sess.add(PortfolioWatchlist(symbol="MEMBER", tier="growth", currency="USD", pending_removal=False,
                                       pending_removal_reason=_reason("20.0", SUBSTANCE)))
    review.sess.flush()
    assert _cards_for(review.run(), "MEMBER") == []


def test_one_card_when_both_growth_and_quality_have_failed(review):
    _dropped(review, "BOTH", _reason("30.0", SUBSTANCE), held_days=200)
    review.trends["BOTH"] = _trend(4.9, 3.4, 2.0)
    review.fundamentals["BOTH"] = {"dividend_yield": 0.0}
    assert _cards_for(review.run(), "BOTH") == [("sell_stock_review", "monthly_growth_thesis_weak")]


def test_dividend_and_breakthrough_names_are_not_judged_on_growth_tier_quality(review):
    from src.portfolio.models import PortfolioWatchlist
    for sym, tier in (("DIVQ", "dividend"), ("BTQ", "breakthrough")):
        review.hold(sym, tier=tier)
        review.sess.add(PortfolioWatchlist(symbol=sym, tier=tier, currency="USD", pending_removal=True,
                                           pending_removal_reason=_reason("10.0", SUBSTANCE)))
    review.sess.flush()
    cards = review.run()
    assert _cards_for(cards, "DIVQ") == [] and _cards_for(cards, "BTQ") == []
