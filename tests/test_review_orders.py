"""Review cards (sell / reduce / covered call from the monthly review) send a real order on the
PORTFOLIO account — but only after a manual approval, and only for what IBKR shows is free to sell.

Three things are pinned here:
  * manual only — no auto-approve switch, bare status change or non-dashboard note sells anything;
  * the broker's position sizes the order — shares under short calls or already in sell orders are
    never sold twice, and an unreadable position sends nothing;
  * the loss-call rule — strike at the break-even or 5% above the price, whichever is higher, and
    the strike actually sent is never below the broker's average cost.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.database as db_mod
import src.portfolio.models  # noqa: F401 (registers the holdings table)
from src.core.models import Base, SystemState
from src.core.suggestions import (REVIEW_MANUAL_APPROVAL_NOTE, TradeSuggestion, approve_suggestion,
                                  create_suggestion)
from src.portfolio import review_orders as ro

MANUAL = REVIEW_MANUAL_APPROVAL_NOTE


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    src.portfolio.models.Base.metadata.create_all(eng)
    monkeypatch.setattr(db_mod, "_engine", eng)
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng))
    return eng


def _stk(symbol="ACME", qty=0, avg=100.0):
    return SimpleNamespace(account="", position=qty, avgCost=avg,
                           contract=SimpleNamespace(secType="STK", symbol=symbol, currency="USD", conId=111))


def _short_call(symbol="ACME", contracts=1):
    return SimpleNamespace(account="", position=-contracts, avgCost=0.0,
                           contract=SimpleNamespace(secType="OPT", symbol=symbol, right="C",
                                                    multiplier="100", currency="USD", conId=333))


def _working(sec="STK", action="SELL", qty=100, symbol="ACME", right=""):
    return SimpleNamespace(
        contract=SimpleNamespace(secType=sec, symbol=symbol, right=right, multiplier="100",
                                 currency="USD", conId=111),
        order=SimpleNamespace(action=action, totalQuantity=qty),
        orderStatus=SimpleNamespace(status="Submitted", remaining=qty))


class FakeIB:
    def __init__(self, positions, open_trades=(), close=100.0, bid=1.00, ask=1.20,
                 strikes=(90, 95, 100, 105, 110, 115, 120), order_status="Submitted",
                 chains=None, listed_strikes=None):
        self._positions, self._open = list(positions), list(open_trades)
        self.close, self.bid, self.ask, self.strikes = close, bid, ask, strikes
        self.order_status = order_status
        self.chains = chains                  # None -> one SMART chain, 100 shares a contract
        self.listed_strikes = listed_strikes  # strikes the broker really lists (default: the chain's)
        self.placed = []

    def positions(self):
        return list(self._positions)

    def openTrades(self):
        return list(self._open)

    def qualifyContracts(self, c):
        c.conId = c.conId or (222 if getattr(c, "secType", "") == "OPT" else 111)
        return [c]

    def reqContractDetails(self, c):
        if getattr(c, "secType", "") == "OPT":
            # the broker's concrete contracts for this expiry/right, like reqContractDetails returns
            exch, tc = c.exchange, c.tradingClass
            return [SimpleNamespace(contract=SimpleNamespace(
                        strike=float(k), conId=5000 + int(k * 10), multiplier=self._mult(exch),
                        tradingClass=tc or "X", lastTradeDateOrContractMonth=c.lastTradeDateOrContractMonth))
                    for k in (self.listed_strikes or self.strikes)]
        return [SimpleNamespace(validExchanges="SMART", minTick=0.01, marketRuleIds="")]

    def _mult(self, exch):
        for ch in self.reqSecDefOptParams():
            if ch.exchange == exch:
                return ch.multiplier
        return "100"

    def reqHistoricalData(self, *a, **k):
        return [SimpleNamespace(close=self.close)]

    def reqSecDefOptParams(self, *a):
        exp = (date.today() + timedelta(days=40)).strftime("%Y%m%d")
        soon = (date.today() + timedelta(days=5)).strftime("%Y%m%d")
        if self.chains is not None:
            return [SimpleNamespace(exchange=ex, tradingClass=tc, multiplier=str(m), expirations=[soon, exp],
                                    strikes=list(self.strikes)) for ex, tc, m in self.chains]
        return [SimpleNamespace(exchange="SMART", tradingClass="X", multiplier="100",
                                expirations=[soon, exp], strikes=list(self.strikes))]

    def reqMktData(self, *a):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    def cancelMktData(self, *a):
        pass

    def sleep(self, *a):
        pass

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(contract=contract, order=order, log=[],
                               orderStatus=SimpleNamespace(status=self.order_status, filled=0, permId=9))


@pytest.fixture
def wired(temp_db, monkeypatch):
    """Portfolio connection replaced by a fake; markets open; account writable."""
    import src.portfolio.buyer as buyer
    import src.portfolio.connection as conn
    from src.core.config import get_settings
    holder = {}
    monkeypatch.setattr(conn, "get_portfolio_ib", lambda: holder["ib"])
    monkeypatch.setattr(conn, "is_portfolio_connected", lambda: True)
    monkeypatch.setattr(conn, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(conn, "refresh_portfolio_pending_orders_cache", lambda: None)
    monkeypatch.setattr(buyer, "_market_open", lambda ccy: True)
    monkeypatch.setattr(ro, "_us_options_open", lambda now=None: True)
    monkeypatch.setattr(ro, "premium_ok", lambda bid, lot, ccy: bool(bid and bid == bid and bid >= 0.20))
    monkeypatch.setattr(get_settings().portfolio, "readonly", False)
    monkeypatch.setattr(get_settings().portfolio, "ibkr_account", "")
    buyer._MIN_TICK_CACHE.clear()
    return holder


def _card(action="sell_stock_review", qty=500, status="approved", note=MANUAL, symbol="ACME", **kw):
    with db_mod.get_db() as db:
        s = TradeSuggestion(symbol=symbol, action=action, status=status, source="rescreen",
                            quantity=qty, review_note=note, reviewed_at=datetime.utcnow(), **kw)
        db.add(s)
        db.flush()
        return s.id


def _get(sid):
    with db_mod.get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.id == sid).first()
        return SimpleNamespace(status=s.status, note=s.review_note or "", quantity=s.quantity,
                               strike=s.strike, limit_price=s.limit_price)


# ── The rules themselves ───────────────────────────────────────────────────────────────────

def test_loss_call_strike_is_break_even_or_five_percent_whichever_is_higher():
    assert ro.loss_call_strike(avg_cost=100, price=90) == 100          # break-even wins
    assert ro.loss_call_strike(avg_cost=100, price=99) == pytest.approx(103.95)   # 5% wins


def test_loss_call_gets_no_card_when_the_break_even_is_out_of_reach():
    assert ro.loss_call_worth_a_card(avg_cost=100, price=85)
    assert not ro.loss_call_worth_a_card(avg_cost=100, price=70)       # 43% away: no bid there


def test_strike_is_never_picked_below_the_floor():
    assert ro.pick_strike([90, 95, 100, 105], 97.5) == 100
    assert ro.pick_strike([90, 95], 97.5) is None


def test_expiry_keeps_clear_of_the_next_two_weeks():
    today = date(2026, 10, 6)
    assert ro.pick_expiry(["20261009", "20261120", "20261218"], "20261120", today) == "20261120"
    assert ro.pick_expiry(["20261009"], "20261009", today) is None


def test_free_shares_exclude_short_calls_and_working_sells():
    book = ro.Book(long_shares=500, short_call_shares=200, working_stock_sells=100)
    assert book.free_shares == 200
    assert ro.sale_size(500, book) == 200
    assert ro.call_size(5, book) == 2
    assert ro.sale_size(500, ro.Book(long_shares=454), lot=100) == 400


# ── Manual only ────────────────────────────────────────────────────────────────────────────

def test_review_card_is_never_auto_approved_and_lives_thirty_days(temp_db):
    with db_mod.get_db() as db:
        db.add(SystemState(key="auto_approve_rescreen", value="true"))
    create_suggestion(symbol="ACME", action="sell_stock_review", quantity=10, limit_price=99.0,
                      source="rescreen", tier="growth", rationale="x", rank=1, expires_hours=720)
    with db_mod.get_db() as db:
        row = db.query(TradeSuggestion).filter(TradeSuggestion.symbol == "ACME").one()
        assert row.status == "pending"
        assert row.expires_at > datetime.utcnow() + timedelta(days=29)


def test_only_the_dashboard_note_approves_a_review_card(temp_db):
    sid = _card(status="pending", note=None)
    assert approve_suggestion(sid, note="auto-approved") is False
    assert approve_suggestion(sid, note="") is False
    assert _get(sid).status == "pending"
    assert approve_suggestion(sid, note=MANUAL) is True
    assert _get(sid).status == "approved"


def test_approved_without_the_manual_marker_sells_nothing(wired):
    wired["ib"] = FakeIB([_stk(qty=500)])
    for note in ("auto-approved", "auto-approved (portfolio auto-execute)", "", None):
        sid = _card(note=note)
        assert ro.execute_review_stock_sale(sid) == "skip"
        assert ro.execute_review_covered_call(_card(action="sell_covered_call_review", qty=1, note=note)) == "skip"
    assert ro.approved_review_cards() == []
    assert wired["ib"].placed == []


def test_pending_card_sells_nothing(wired):
    wired["ib"] = FakeIB([_stk(qty=500)])
    assert ro.execute_review_stock_sale(_card(status="pending", note=MANUAL)) == "skip"
    assert wired["ib"].placed == []


# ── Stock sale ─────────────────────────────────────────────────────────────────────────────

def test_sale_is_clamped_to_what_the_broker_shows_free(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=300), _short_call(contracts=1)], close=100.0)
    sid = _card(qty=500)                       # card is a month old and says 500
    assert ro.execute_review_stock_sale(sid) == "submitted"
    (contract, order), = ib.placed
    assert (order.action, order.totalQuantity, order.tif) == ("SELL", 200, "DAY")
    assert order.lmtPrice == pytest.approx(99.80)          # last trade less 0.2%
    card = _get(sid)
    assert card.quantity == 200 and "position before 300" in card.note


def test_working_sell_order_is_not_sold_again(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=300)], open_trades=[_working(qty=300)])
    sid = _card(qty=300)
    assert ro.execute_review_stock_sale(sid) == "rejected"
    assert ib.placed == [] and "already in sell orders" in _get(sid).note


def test_no_position_at_the_broker_sends_nothing(wired):
    ib = wired["ib"] = FakeIB([_stk(symbol="OTHER", qty=50)])
    sid = _card()
    assert ro.execute_review_stock_sale(sid) == "rejected"
    assert ib.placed == []


def test_unreadable_positions_send_nothing_and_stay_manual(wired):
    ib = wired["ib"] = FakeIB([])              # positions not loaded
    sid = _card()
    assert ro.execute_review_stock_sale(sid) == "approved"
    assert ib.placed == []
    assert _get(sid).note.startswith(MANUAL)   # still a hand approval on the retry


def test_market_shut_waits_without_sending(wired, monkeypatch):
    import src.portfolio.buyer as buyer
    monkeypatch.setattr(buyer, "_market_open", lambda ccy: False)
    ib = wired["ib"] = FakeIB([_stk(qty=300)])
    sid = _card()
    assert ro.execute_review_stock_sale(sid) == "approved"
    assert ib.placed == [] and "market opens" in _get(sid).note


def test_order_the_broker_refuses_returns_the_card_for_a_new_approval(wired):
    wired["ib"] = FakeIB([_stk(qty=300)], order_status="Inactive")
    sid = _card(qty=300)
    assert ro.execute_review_stock_sale(sid) == "pending"
    assert ro.approved_review_cards() == []          # never retried on its own


# ── Covered call ───────────────────────────────────────────────────────────────────────────

def test_call_strike_is_never_below_the_brokers_average_cost(wired):
    # Card said 100 a month ago; the broker's average cost is 108; price 95.
    ib = wired["ib"] = FakeIB([_stk(qty=250, avg=108.0)], close=95.0, bid=1.00, ask=1.20)
    sid = _card(action="sell_covered_call_review", qty=3, strike=100.0,
                expiry=(date.today() + timedelta(days=40)).strftime("%Y%m%d"), right="C")
    assert ro.execute_review_covered_call(sid) == "submitted"
    (opt, order), = ib.placed
    assert opt.strike == 110 and opt.right == "C"           # first listed strike at/above 108
    assert (order.action, order.totalQuantity) == ("SELL", 2)   # 250 shares cover two, not three
    assert order.lmtPrice == 1.00                           # at the bid, like the option side
    assert _get(sid).strike == 110


def test_call_keeps_five_percent_above_todays_price(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=100, avg=50.0)], close=100.0)
    sid = _card(action="sell_covered_call_review", qty=1, strike=90.0, right="C")
    assert ro.execute_review_covered_call(sid) == "submitted"
    assert ib.placed[0][0].strike == 105


def test_shares_already_under_a_call_get_no_second_call(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=100), _short_call(contracts=1)])
    sid = _card(action="sell_covered_call_review", qty=1, strike=110.0, right="C")
    assert ro.execute_review_covered_call(sid) == "rejected"
    assert ib.placed == []


def test_sale_and_call_approved_together_cannot_both_take_the_shares(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=100)], open_trades=[_working(qty=100)])   # the sale is working
    sid = _card(action="sell_covered_call_review", qty=1, strike=110.0, right="C")
    assert ro.execute_review_covered_call(sid) == "rejected"
    assert ib.placed == []


def test_call_without_a_bid_is_not_sent(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=100)], bid=0.15, ask=0.30)      # under the $0.20 floor
    sid = _card(action="sell_covered_call_review", qty=1, strike=110.0, right="C")
    assert ro.execute_review_covered_call(sid) == "approved"
    assert ib.placed == [] and "no worthwhile bid" in _get(sid).note


def test_listing_without_an_option_market_gets_no_call(wired):
    from src.portfolio.models import PortfolioHolding
    with db_mod.get_db() as db:
        db.add(PortfolioHolding(symbol="035420", exchange="KRX", currency="KRW", shares=500))
    ib = wired["ib"] = FakeIB([_stk(symbol="035420", qty=500)])
    sid = _card(action="sell_covered_call_review", qty=5, strike=110.0, right="C", symbol="035420")
    assert ro.execute_review_covered_call(sid) == "rejected"
    assert ib.placed == []


def test_hong_kong_call_uses_the_brokers_500_share_contract(wired):
    from src.portfolio.models import PortfolioHolding
    with db_mod.get_db() as db:
        db.add(PortfolioHolding(symbol="3690", exchange="SEHK", currency="HKD", shares=1200))
    ib = wired["ib"] = FakeIB([_stk(symbol="3690", qty=1200, avg=74.9)], close=70.85,
                              chains=[("SEHK", "MET", 500)], strikes=(70, 72.5, 75, 77, 80, 85))
    sid = _card(action="sell_covered_call_review", qty=12, strike=77.0, right="C", symbol="3690")
    assert ro.execute_review_covered_call(sid) == "submitted"
    (opt, order), = ib.placed
    assert order.totalQuantity == 2                       # 1200 shares cover two 500-share contracts, not 12
    assert opt.exchange == "SEHK" and opt.tradingClass == "MET" and opt.multiplier == "500"
    assert opt.strike == 77.0 and opt.conId > 0           # the broker's own contract, not a guess
    assert "500 sh/contract" in _get(sid).note


def test_chain_choice_skips_a_class_the_position_cannot_cover():
    azn = [SimpleNamespace(exchange="ICEEU", tradingClass="AZA", multiplier="1000", expirations=list(range(60))),
           SimpleNamespace(exchange="ICEEU", tradingClass="8ZA", multiplier="100", expirations=list(range(6)))]
    assert ro.pick_chain(azn, 815, "GBP").tradingClass == "8ZA"        # 1,000-share class useless for 815
    asml = [SimpleNamespace(exchange="EUREX", tradingClass="ASM", multiplier="100", expirations=list(range(17))),
            SimpleNamespace(exchange="FTA", tradingClass="AS9C", multiplier="100", expirations=list(range(7))),
            SimpleNamespace(exchange="EUREX", tradingClass="ASM2", multiplier="10", expirations=list(range(17)))]
    assert ro.pick_chain(asml, 100, "EUR").tradingClass == "ASM"        # deepest full-size class
    us = [SimpleNamespace(exchange="CBOE", multiplier="100", expirations=[]),
          SimpleNamespace(exchange="SMART", multiplier="100", expirations=[])]
    assert ro.pick_chain(us, 100, "USD").exchange == "SMART"
    assert ro.pick_chain(azn, 50, "GBP") is None


def test_premium_floor_is_judged_in_base_currency(monkeypatch):
    import src.portfolio.fx as fx
    monkeypatch.setattr(fx, "load_fx_rates", lambda: {"EUR": 1.0, "USD": 0.89, "GBP": 1.18, "HKD": 0.114})
    assert ro.premium_ok(0.20, 100, "USD")                 # $20 a contract ~ EUR 17.8
    assert not ro.premium_ok(0.10, 100, "USD")
    assert ro.premium_ok(0.30, 500, "HKD")                 # HK$150 ~ EUR 17
    assert not ro.premium_ok(0.10, 500, "HKD")
    assert ro.premium_ok(15.0, 100, "GBP")                 # 15 PENCE x 100 = GBP 15 ~ EUR 17.7
    assert not ro.premium_ok(0.0, 100, "USD") and not ro.premium_ok(float("nan"), 100, "USD")


# ── After the order ────────────────────────────────────────────────────────────────────────

def _sent(note, qty=300, action="sell_stock_review"):
    with db_mod.get_db() as db:
        s = TradeSuggestion(symbol="ACME", action=action, status="submitted", source="rescreen",
                            quantity=qty, review_note=note,
                            reviewed_at=datetime.utcnow() - timedelta(minutes=10))
        db.add(s)
        db.flush()
        return s.id


def test_unfilled_order_returns_to_pending_and_is_not_resent(wired):
    ib = wired["ib"] = FakeIB([_stk(qty=300)])
    sid = _sent("Sent to IBKR: SELL 300 @ 99.8, good for today (order 7; position before 300)")
    ro.reconcile_review_orders()
    assert _get(sid).status == "pending"
    assert ro.approved_review_cards() == [] and ib.placed == []


def test_filled_order_is_marked_executed(wired):
    wired["ib"] = FakeIB([_stk(symbol="OTHER", qty=10)])       # ACME gone from the account
    sid = _sent("Sent to IBKR: SELL 300 @ 99.8, good for today (order 7; position before 300)")
    ro.reconcile_review_orders()
    assert _get(sid).status == "executed"


def test_partly_filled_order_asks_again_for_the_rest_only(wired):
    wired["ib"] = FakeIB([_stk(qty=180)])
    sid = _sent("Sent to IBKR: SELL 300 @ 99.8, good for today (order 7; position before 300)")
    ro.reconcile_review_orders()
    card = _get(sid)
    assert card.status == "pending" and card.quantity == 180 and card.note.startswith("Partly filled")


def test_order_still_working_is_left_alone(wired):
    wired["ib"] = FakeIB([_stk(qty=300)], open_trades=[_working(qty=300)])
    sid = _sent("Sent to IBKR: SELL 300 @ 99.8, good for today (order 7; position before 300)")
    ro.reconcile_review_orders()
    assert _get(sid).status == "submitted"


def test_sold_names_are_not_bought_back(temp_db):
    now = datetime.utcnow()
    with db_mod.get_db() as db:
        def add(sym, status, note="", when=now, action="sell_stock_review"):
            db.add(TradeSuggestion(symbol=sym, action=action, status=status, source="rescreen",
                                   quantity=1, review_note=note, reviewed_at=when))
        add("SOLD", "executed", "Filled: sold 1")
        add("OLD", "executed", "Filled: sold 1", when=now - timedelta(days=120))
        add("WORKING", "submitted")
        add("APPROVED", "approved", MANUAL)
        add("PARTLY", "pending", "Partly filled: sold 1 of 2 shares")
        add("JUSTACARD", "pending")
        add("NOTED", "approved", "")                      # old-style acknowledgement, not a sale
        add("CALL", "executed", action="sell_covered_call_review")
    assert ro.review_sale_blocked_symbols() == {"SOLD", "WORKING", "APPROVED", "PARTLY"}


# ── The card the monthly review writes ─────────────────────────────────────────────────────

def _facts(**over):
    base = {"shares": 300, "avg_cost": 100.0, "price": 92.0, "tier": "growth", "currency": "USD", "sma_200": 95.0}
    base.update(over)
    return base


def test_exit_call_plan_has_three_tiers():
    today = date(2026, 10, 7)
    gain = ro.exit_call_plan(100.0, 120.0, today)
    assert gain["tier"] == "profit" and gain["strike"] == 130                 # ceil(120 x 1.08)
    near = ro.exit_call_plan(100.0, 99.0, today)
    assert near["tier"] == "breakeven" and near["strike"] == 104              # 5% up beats break-even
    loss = ro.exit_call_plan(100.0, 92.0, today)
    assert loss["tier"] == "breakeven" and loss["strike"] == 100              # break-even within 20%
    deep = ro.exit_call_plan(100.0, 60.0, today)
    assert deep["tier"] == "deep" and deep["strike"] == 68                    # ceil(60 x 1.12), below cost
    assert deep["called_pnl_pct"] == pytest.approx(-32.0) and deep["pnl_pct"] == pytest.approx(-40.0)
    assert deep["expiry"] > loss["expiry"]                                    # a month further out
    assert ro.exit_call_plan(0, 50.0, today) is None
    assert ro.exit_call_plan(100.0, float("nan"), today) is None              # a shut market quotes NaN


def test_review_writes_a_call_card_beside_every_sell_or_reduce_card(monkeypatch):
    import src.core.suggestions as sugg
    from src.portfolio.scheduler import _exit_call_cards
    made = []
    monkeypatch.setattr(sugg, "create_suggestion", lambda **kw: made.append(kw))
    facts = {
        "LOSS": _facts(shares=250),                       # break-even within reach
        "NEAR": _facts(shares=100, price=99.0),
        "GAIN": _facts(price=120.0),
        "DEEP": _facts(price=60.0),                       # break-even out of reach -> deep tier
        "TRIM": _facts(shares=1000, price=130.0),         # REDUCE card trimming 400 shares
        "ODD":  _facts(shares=60),                        # under one contract
        "XRO":  _facts(shares=500, currency="AUD", exchange="ASX"),   # Australian options exist
        "HASCC": _facts(shares=500),                      # call card already open
        "UNFLAGGED": _facts(shares=500),
    }
    facts["MEIT"] = _facts(shares=1200, price=70.0, avg_cost=75.0, currency="HKD", exchange="SEHK")   # 500-share lots
    facts["NAVER"] = _facts(shares=500, currency="KRW", exchange="KRX")                             # no option market
    monkeypatch.setattr(ro, "option_lot", lambda ib, sym, ex, ccy, shares: 500 if ccy == "HKD" else 100)
    flagged = [{"symbol": s, "action": "CONSIDER SELL"} for s in facts if s not in ("UNFLAGGED", "TRIM")]
    flagged.append({"symbol": "TRIM", "action": "REDUCE", "shares": 400})
    out = _exit_call_cards(flagged, facts, open_cc_symbols={"HASCC"}, ib=object())

    by = {kw["symbol"]: kw for kw in made}
    assert set(by) == {"LOSS", "NEAR", "GAIN", "DEEP", "TRIM", "XRO", "MEIT"}       # Australia and Hong Kong now included
    assert by["MEIT"]["quantity"] == 2                                              # 1200 shares / 500 a contract
    assert by["XRO"]["quantity"] == 5
    assert by["LOSS"]["strike"] == 100 and by["LOSS"]["quantity"] == 2 and by["LOSS"]["signal"] == "monthly_exit_call_breakeven"
    assert by["NEAR"]["strike"] == 104
    assert by["GAIN"]["strike"] == 130 and by["GAIN"]["signal"] == "monthly_exit_call_profit"
    assert by["DEEP"]["strike"] == 68 and by["DEEP"]["signal"] == ro.EXIT_CALL_DEEP_SIGNAL
    assert "-32.0% instead of -40.0%" in by["DEEP"]["rationale"]             # the card says the number
    assert by["TRIM"]["quantity"] == 4                                        # covers the trim, not the lot
    assert all(kw["action"] == "sell_covered_call_review" and kw["source"] == "rescreen" for kw in made)
    assert {o["symbol"] for o in out} == set(by)


def test_deep_tier_card_is_not_raised_to_the_average_cost(wired):
    # Cost 100, price 60: the card offers 68 on purpose. The executor must not lift it to 100
    # (no bid there) — only to the card's strike or 5% above today's price, whichever is higher.
    ib = wired["ib"] = FakeIB([_stk(qty=300, avg=100.0)], close=60.0, strikes=(60, 65, 70, 75, 100, 105))
    sid = _card(action="sell_covered_call_review", qty=3, strike=68.0, right="C", signal=ro.EXIT_CALL_DEEP_SIGNAL)
    assert ro.execute_review_covered_call(sid) == "submitted"
    assert ib.placed[0][0].strike == 70                                       # first listed strike >= 68
    # The same position on a break-even card IS lifted to cost.
    ib = wired["ib"] = FakeIB([_stk(qty=300, avg=100.0)], close=60.0, strikes=(60, 65, 70, 75, 100, 105))
    sid = _card(action="sell_covered_call_review", qty=3, strike=68.0, right="C", signal="monthly_exit_call_breakeven")
    assert ro.execute_review_covered_call(sid) == "submitted"
    assert ib.placed[0][0].strike == 100


def test_one_pass_sends_one_order_per_name_and_skips_cards_that_must_wait(wired, monkeypatch):
    import src.portfolio.buyer as buyer
    from src.portfolio.models import PortfolioHolding
    with db_mod.get_db() as db:
        db.add(PortfolioHolding(symbol="6920", exchange="TSEJ", currency="JPY", shares=100))
    monkeypatch.setattr(buyer, "_market_open", lambda ccy: ccy == "USD")     # Tokyo shut
    ib = wired["ib"] = FakeIB([_stk(qty=100), _stk(symbol="6920", qty=100)])
    tokyo = _card(symbol="6920", qty=100)                                   # oldest, must wait
    sale = _card(qty=100)
    call = _card(action="sell_covered_call_review", qty=1, strike=110.0, right="C")
    ro.run_review_orders()
    assert _get(tokyo).status == "approved"                                 # waiting, not blocking
    assert _get(sale).status == "submitted"
    assert _get(call).status == "approved"                                  # same name: next pass
    assert len(ib.placed) == 1 and ib.placed[0][1].totalQuantity == 100


def test_watchlist_labels_a_sold_name_instead_of_buy(temp_db):
    """The page must say what the buyer does: a name sold on review is skipped, not a "buy"."""
    from src.portfolio import compounder as cmp
    from src.portfolio.config import PortfolioConfig
    CC = PortfolioConfig().compounder
    NLV = 10_000_000.0
    TIER_ALLOC = {"breakthrough": CC.tier_breakthrough, "dividend": CC.tier_dividend,
                  "growth": CC.tier_growth}

    def _row(symbol, sector):
        return SimpleNamespace(
            symbol=symbol, tier="growth", sector=sector, currency="USD", current_price=100.0,
            growth_score=70.0, forward_growth_score=70.0, quality_score=70.0, valuation_score=70.0,
            dividend_total_return_score=70.0, risk_total_penalty=0.0, sma_200=110.0, high_52w=150.0,
            momentum_12_1=0.2, pending_removal=False, category="growth")
    rows = [_row("AAA", "Technology"), _row("BBB", "Healthcare")]
    plain = {s["symbol"]: s for s in cmp.build_signals_from_watchlist(rows, {}, NLV, CC, TIER_ALLOC)}
    assert plain["AAA"]["action"] in ("direct", "fill")
    sold = {s["symbol"]: s for s in cmp.build_signals_from_watchlist(
        rows, {}, NLV, CC, TIER_ALLOC, review_sold={"AAA": "Sold on a review card"})}
    assert sold["AAA"]["action"] == "review_sold" and sold["AAA"]["action_note"] == "Sold on a review card"
    assert sold["AAA"]["target"] == plain["AAA"]["target"]            # label only — targets untouched
    assert sold["BBB"]["action"] == plain["BBB"]["action"] and "action_note" not in sold["BBB"]


def test_block_reasons_say_until_when(temp_db):
    when = datetime(2026, 10, 6, 14, 0)
    with db_mod.get_db() as db:
        db.add(TradeSuggestion(symbol="SOLD", action="sell_stock_review", status="executed",
                               source="rescreen", quantity=1, review_note="Filled", reviewed_at=when))
    blocks = ro.review_sale_blocks(days=36500)
    assert "2026-10-06" in blocks["SOLD"]
