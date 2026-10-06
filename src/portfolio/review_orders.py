"""
Orders for monthly-review cards — PORTFOLIO account only, and only after a manual approval.

The monthly review (src/portfolio/scheduler.py) writes three kinds of card: sell the position
(sell_stock_review), trim it (reduce_position_review), or sell a covered call on it
(sell_covered_call_review). Until now approving one only marked it "noted". This module turns an
approved card into a real order on the portfolio connection.

Three rules hold everywhere below:

  1. MANUAL ONLY. A card is acted on only when its status is "approved" AND its note carries the
     marker the dashboard's Approve button writes (core.suggestions.REVIEW_MANUAL_APPROVAL_NOTE).
     Nothing else in the system writes that marker, so no auto-approve switch, promotion job or
     stray status change can cause a sale. One approval sends ONE order, good for the day; if it
     ends unfilled the card returns to "pending" and waits for a new approval.

  2. THE BROKER'S POSITION DECIDES THE SIZE. Before any order the live IBKR position is read:
     shares held, minus shares already pledged to short calls, minus shares in working sell
     orders. The order is clamped to that; if the position cannot be read nothing is sent. The
     card's own quantity is only an upper bound — it may be a month old.

  3. NOTHING HERE TOUCHES THE OPTIONS ACCOUNT. Every call goes through the portfolio connection
     and its lock.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ib_insync import LimitOrder, Option

from src.core import quote_units as qu
from src.core.database import get_db
from src.core.logger import get_logger
from src.portfolio.symbols import broker_stock, broker_symbol, internal_symbol

log = get_logger(__name__)

STOCK_ACTIONS = ("sell_stock_review", "reduce_position_review")
CALL_ACTION = "sell_covered_call_review"

# A sale goes out as a limit a hair under the last trade: marketable in a normal book, never a
# market order.
SELL_LIMIT_DISCOUNT = 0.002
# Exit-call rule (Rain, 2026-10-07): a call card beside EVERY sell/reduce card. A long-term holder
# who has decided to leave a name does better selling a call above today's price than selling
# today: called away, the stock went for more plus the premium; not called, the shares and the
# premium stay. Three tiers by where the price stands against cost:
CALL_OTM_PCT = 0.05            # below cost: strike at break-even, or 5% up if that is higher
EXIT_CALL_OTM_PROFIT = 0.08    # above cost: strike 8% up — paid to wait for a bit more
EXIT_CALL_OTM_DEEP = 0.12      # deep below cost: strike 12% up — a smaller loss than selling now
BREAKEVEN_REACH_PCT = 0.20     # break-even further than this has no bid: use the deep tier
LOSS_CALL_MAX_OTM_PCT = BREAKEVEN_REACH_PCT
EXIT_CALL_DEEP_SIGNAL = "monthly_exit_call_deep"   # the one tier whose strike sits BELOW cost
MIN_CALL_BID = 0.20        # same floor the option side uses for a covered call (min_premium)
MIN_CALL_DTE = 14
# A name sold on an approved review card is not bought back by the compounder for this long.
REBUY_BLOCK_DAYS = 90
# Transient failures (no quote yet, positions not loaded) are retried this many 60s cycles, then
# the card goes back to "pending" rather than retrying for ever.
MAX_ATTEMPTS = 10
# A just-placed order is not judged "ended" before its status has had time to arrive.
RECONCILE_GRACE_SECONDS = 90

_DONE = ("Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected")
_PARTLY = "Partly filled"


# ── Pure rules ─────────────────────────────────────────────────────────────────────────────

def loss_call_strike(avg_cost: float, price: float) -> float:
    """Strike target for a covered call on a position held at a loss: the break-even, or 5%
    above the price if that is higher. Called away at this strike, the stock leaves at no loss."""
    return max(float(avg_cost or 0), float(price or 0) * (1 + CALL_OTM_PCT))


def loss_call_worth_a_card(avg_cost: float, price: float) -> bool:
    """False when the break-even sits so far above the price that no call there has a bid."""
    if not price or price <= 0 or not avg_cost or avg_cost <= 0:
        return False
    return loss_call_strike(avg_cost, price) <= price * (1 + LOSS_CALL_MAX_OTM_PCT)


def exit_call_plan(avg_cost: float, price: float, today: date) -> dict | None:
    """Strike, expiry and tier of the covered call offered beside an exit card.

    profit    — price at or above cost: strike 8% above the price, ~45-60 days out.
    breakeven — below cost but the break-even is within 20%: strike at break-even (or 5% up),
                ~45-60 days; called away means out at no loss.
    deep      — break-even further than 20% away (a call there has no bid): strike 12% above the
                price, a month further out so it still earns something. Called away realises a
                smaller loss than selling today; the card says the number.
    None when there is no usable price or cost."""
    if not price or price <= 0 or not avg_cost or avg_cost <= 0:
        return None
    if price >= avg_cost:
        tier, strike, expiry = "profit", price * (1 + EXIT_CALL_OTM_PROFIT), review_call_expiry(today)
    elif avg_cost <= price * (1 + BREAKEVEN_REACH_PCT):
        tier, strike, expiry = "breakeven", loss_call_strike(avg_cost, price), review_call_expiry(today)
    else:
        tier, strike, expiry = "deep", price * (1 + EXIT_CALL_OTM_DEEP), review_call_expiry(today + timedelta(days=30))
    strike = float(math.ceil(strike))
    return {"tier": tier, "strike": strike, "expiry": expiry,
            "signal": EXIT_CALL_DEEP_SIGNAL if tier == "deep" else f"monthly_exit_call_{tier}",
            "pnl_pct": (price / avg_cost - 1) * 100,
            "called_pnl_pct": (strike / avg_cost - 1) * 100}


def review_call_expiry(today: date) -> date:
    """Expiry the monthly review proposes for a covered call: the third Friday of next month,
    or of the month after once today is past the 15th."""
    first = today.replace(day=1)

    def _next_month(d: date) -> date:
        return d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)

    target = _next_month(_next_month(first) if today.day > 15 else first)
    first_friday = target + timedelta(days=(4 - target.weekday()) % 7)
    return first_friday + timedelta(weeks=2)


def pick_strike(strikes, floor: float) -> float | None:
    """Lowest listed strike at or above `floor` — never below it."""
    ok = sorted(s for s in strikes if s >= floor - 1e-9)
    return ok[0] if ok else None


def pick_expiry(expirations, wanted: str | None, today: date) -> str | None:
    """Listed expiry closest to the card's, at least MIN_CALL_DTE days out."""
    try:
        want = datetime.strptime(wanted, "%Y%m%d").date() if wanted else today + timedelta(days=35)
    except ValueError:
        want = today + timedelta(days=35)
    best, best_gap = None, None
    for e in expirations:
        try:
            d = datetime.strptime(e, "%Y%m%d").date()
        except ValueError:
            continue
        if (d - today).days < MIN_CALL_DTE:
            continue
        gap = abs((d - want).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = e, gap
    return best


@dataclass
class Book:
    """What IBKR shows for one name, in shares."""
    long_shares: int = 0
    avg_cost: float = 0.0
    short_call_shares: int = 0          # shares pledged to short calls already held
    working_stock_sells: int = 0        # shares in working SELL orders
    working_call_sells: int = 0         # shares in working SELL-call orders

    @property
    def free_shares(self) -> int:
        """Shares that can be sold, or have a call written on them, without going short/naked."""
        return max(0, self.long_shares - self.short_call_shares
                   - self.working_stock_sells - self.working_call_sells)


def sale_size(card_qty: int, book: Book, lot: int = 1) -> int:
    qty = min(int(card_qty or 0), book.free_shares)
    if lot > 1:
        qty = (qty // lot) * lot
    return max(0, qty)


def call_size(card_contracts: int, book: Book) -> int:
    return max(0, min(int(card_contracts or 0), book.free_shares // 100))


# ── Reading the broker ─────────────────────────────────────────────────────────────────────

def read_book(ib, symbol: str, account: str = "") -> Book | None:
    """Live position and working sell orders for `symbol`. None when positions cannot be read —
    callers must then send nothing."""
    from src.portfolio.connection import get_portfolio_lock
    try:
        with get_portfolio_lock():
            positions = list(ib.positions())
            open_trades = list(ib.openTrades())
    except Exception as e:
        log.warning("review_order_positions_unreadable", symbol=symbol, error=str(e))
        return None
    if not positions:
        return None                       # an account with holdings never reports none: not loaded
    underlying = broker_symbol(symbol)
    book = Book()
    for p in positions:
        if account and getattr(p, "account", "") and p.account != account:
            continue
        c = p.contract
        sec = getattr(c, "secType", "")
        if sec == "STK" and internal_symbol(c) == symbol:
            book.long_shares += int(p.position or 0)
            book.avg_cost = float(getattr(p, "avgCost", 0) or 0)
        elif sec == "OPT" and getattr(c, "right", "") == "C" and c.symbol == underlying \
                and (p.position or 0) < 0:
            book.short_call_shares += int(abs(p.position) * int(float(c.multiplier or 100)))
    for t in open_trades:
        c, o, st = t.contract, t.order, t.orderStatus
        if (getattr(st, "status", "") or "") in _DONE or str(getattr(o, "action", "")).upper() != "SELL":
            continue
        left = float(getattr(st, "remaining", 0) or 0) or float(getattr(o, "totalQuantity", 0) or 0)
        sec = getattr(c, "secType", "")
        if sec == "STK" and internal_symbol(c) == symbol:
            book.working_stock_sells += int(left)
        elif sec == "OPT" and getattr(c, "right", "") == "C" and c.symbol == underlying:
            book.working_call_sells += int(left * int(float(getattr(c, "multiplier", 100) or 100)))
    return book


def _last_price(ib, contract) -> float | None:
    """Last trade (else midpoint) for a qualified contract, in the unit the venue quotes."""
    from src.portfolio.connection import get_portfolio_lock
    for what in ("TRADES", "MIDPOINT"):
        try:
            with get_portfolio_lock():
                bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="2 D",
                                            barSizeSetting="1 day", whatToShow=what, useRTH=False,
                                            formatDate=1, timeout=8)
            if bars and bars[-1].close and bars[-1].close > 0:
                return float(bars[-1].close)
        except Exception as e:
            log.debug("review_order_price_failed", what=what, error=str(e) or repr(e))
    return None


def _us_options_open(now: datetime | None = None) -> bool:
    import pytz
    et = (now or datetime.now(pytz.utc)).astimezone(pytz.timezone("US/Eastern"))
    return et.weekday() < 5 and (9, 35) <= (et.hour, et.minute) < (15, 55)


def _order_error(trade) -> str:
    for entry in reversed(getattr(trade, "log", None) or []):
        if getattr(entry, "message", ""):
            return entry.message
    return ""


# ── Card state ─────────────────────────────────────────────────────────────────────────────

def _waiting(reason: str) -> str:
    """Note for a card still approved and not yet sent. Keeps the manual-approval marker."""
    from src.core.suggestions import REVIEW_MANUAL_APPROVAL_NOTE
    return f"{REVIEW_MANUAL_APPROVAL_NOTE} — {reason}"


def _set(suggestion_id: int, status: str, note: str, **fields) -> str:
    from src.core.suggestions import TradeSuggestion
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
        if s:
            s.status = status
            s.review_note = note
            for k, v in fields.items():
                setattr(s, k, v)
    return status


def _retry_or_release(suggestion_id: int, reason: str) -> str:
    """Transient obstacle: stay approved and try again next cycle; after MAX_ATTEMPTS hand the card
    back for a fresh approval instead of retrying for ever."""
    from src.core.suggestions import TradeSuggestion
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
        if not s:
            return "skip"
        s.funding_attempts = (s.funding_attempts or 0) + 1
        if s.funding_attempts >= MAX_ATTEMPTS:
            s.status, s.funding_attempts = "pending", 0
            s.review_note = f"Nothing sent: {reason}. Approve again to retry."
        else:
            s.status = "approved"
            s.review_note = _waiting(f"{reason}; will retry")
        return s.status


def _claim(suggestion_id: int) -> bool:
    """approved -> executing, atomically, so two passes can never both send the order."""
    from src.core.suggestions import TradeSuggestion
    with get_db() as db:
        n = db.query(TradeSuggestion).filter(
            TradeSuggestion.id == suggestion_id, TradeSuggestion.status == "approved",
        ).update({"status": "executing"}, synchronize_session=False)
        return n == 1


def _load(suggestion_id: int, actions) -> dict | None:
    """The card's fields if — and only if — it is a hand-approved review card of `actions`."""
    from src.core.suggestions import TradeSuggestion, is_manual_review_approval
    from src.portfolio.models import PortfolioHolding, PortfolioWatchlist
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
        if not s or s.action not in actions or s.status != "approved" \
                or not is_manual_review_approval(s.review_note):
            return None
        row = (db.query(PortfolioHolding).filter(PortfolioHolding.symbol == s.symbol).first()
               or db.query(PortfolioWatchlist).filter(PortfolioWatchlist.symbol == s.symbol).first())
        return {"symbol": s.symbol, "action": s.action, "quantity": int(s.quantity or 0),
                "strike": s.strike, "expiry": s.expiry, "signal": s.signal or "",
                "exchange": (row.exchange if row and row.exchange else "SMART"),
                "currency": (row.currency if row and row.currency else "USD")}


def _preflight(suggestion_id: int) -> str | None:
    """Account-level reasons to send nothing right now. Returns the card status, or None to go on."""
    from src.core.config import get_settings
    from src.portfolio.connection import is_portfolio_connected
    if getattr(get_settings().portfolio, "readonly", False):
        return _set(suggestion_id, "approved", _waiting("portfolio account is read-only, nothing sent"))
    if not is_portfolio_connected():
        return _set(suggestion_id, "approved", _waiting("portfolio IBKR disconnected; will retry"))
    return None


# ── Stock sale ─────────────────────────────────────────────────────────────────────────────

def execute_review_stock_sale(suggestion_id: int) -> str:
    """Send the SELL limit order for a hand-approved sell / reduce review card."""
    from src.core.config import get_settings
    from src.portfolio import buyer as b
    from src.portfolio.connection import (_ensure_event_loop, get_portfolio_ib, get_portfolio_lock,
                                          refresh_portfolio_pending_orders_cache)
    card = _load(suggestion_id, STOCK_ACTIONS)
    if card is None:
        return "skip"
    blocked = _preflight(suggestion_id)
    if blocked:
        return blocked
    symbol, exch, ccy = card["symbol"], card["exchange"], card["currency"]
    if not b._market_open(ccy):
        return _set(suggestion_id, "approved", _waiting("order goes out when the market opens"))
    if not _claim(suggestion_id):
        return "skip"

    try:
        _ensure_event_loop()
        ib = get_portfolio_ib()
        book = read_book(ib, symbol, get_settings().portfolio.ibkr_account)
        if book is None:
            return _retry_or_release(suggestion_id, "IBKR positions could not be read")

        # Same qualification and routing as the buy side (see execute_portfolio_buy_suggestion).
        details = None
        if exch and exch != "SMART":
            contract = broker_stock(symbol, exch, ccy)
            with get_portfolio_lock():
                qualified = ib.qualifyContracts(contract)
                details = ib.reqContractDetails(broker_stock(symbol, exch, ccy))
            if not qualified or not details:
                return _retry_or_release(suggestion_id, f"contract not resolved on {exch}")
            valid = [e.strip() for e in (details[0].validExchanges or "").split(",") if e.strip()]
            contract.exchange = exch if exch in b._SMART_HANGS_EXCH else ("SMART" if "SMART" in valid else exch)
        else:
            contract = broker_stock(symbol, "SMART", ccy)
            with get_portfolio_lock():
                qualified = ib.qualifyContracts(contract)
            if not qualified:
                return _retry_or_release(suggestion_id, "contract not resolved")

        qty = sale_size(card["quantity"], book, b._board_lot(details, ccy))
        if qty <= 0:
            log.warning("review_sale_nothing_sellable", id=suggestion_id, symbol=symbol,
                        long=book.long_shares, short_call_shares=book.short_call_shares,
                        working_sells=book.working_stock_sells + book.working_call_sells)
            return _set(suggestion_id, "rejected",
                        f"Nothing sent: IBKR shows {book.long_shares} shares, "
                        f"{book.short_call_shares} pledged to short calls, "
                        f"{book.working_stock_sells + book.working_call_sells} already in sell orders.",
                        reviewed_at=datetime.utcnow())

        ref = _last_price(ib, contract)
        if not ref:
            return _retry_or_release(suggestion_id, "no price from IBKR")
        raw = ref * (1 - SELL_LIMIT_DISCOUNT)
        tick = b._effective_tick(ib, contract, raw, details=details, currency=ccy,
                                 exchange=getattr(contract, "exchange", None))
        order_price = b._round_to_tick(raw, tick, "SELL")

        order = LimitOrder("SELL", qty, order_price)
        order.tif = "DAY"
        order.outsideRth = b._outside_rth_ok(ccy)
        with get_portfolio_lock():
            trade = ib.placeOrder(contract, order)
        status, _filled = b._await_order_outcome(
            ib, trade, qty, timeout=8.0, until=b._ORDER_DONE_STATES + ("Submitted", "PreSubmitted"))
        status = status or "Submitted"
        try:
            refresh_portfolio_pending_orders_cache()
        except Exception:
            pass

        ack = b._order_ack_fields(trade)
        price_major = round(qu.quote_to_major(order_price, ccy), 4)
        log.info("review_sale_order_placed", id=suggestion_id, symbol=symbol, shares=qty,
                 price=order_price, ref=ref, currency=ccy, route=getattr(contract, "exchange", ""),
                 order_status=status, position_before=book.long_shares, card_qty=card["quantity"],
                 perm_id=ack.get("perm_id"), order_id=ack.get("order_id"))
        now = datetime.utcnow()
        if status == "Filled":
            return _set(suggestion_id, "executed", f"Filled: sold {qty} @ {order_price}",
                        quantity=qty, limit_price=price_major, reviewed_at=now)
        if status in ("Cancelled", "ApiCancelled", "Inactive", "Rejected"):
            why = _order_error(trade)
            return _set(suggestion_id, "pending",
                        f"IBKR did not take the order ({status}{': ' + why if why else ''}) — nothing "
                        f"sold. Approve again to re-send.", reviewed_at=now)
        return _set(suggestion_id, "submitted",
                    f"Sent to IBKR: SELL {qty} @ {order_price}, good for today "
                    f"(order {ack.get('order_id')}; position before {book.long_shares})",
                    quantity=qty, limit_price=price_major, reviewed_at=now)
    except Exception as e:
        err = str(e) or type(e).__name__
        log.error("review_sale_execution_failed", id=suggestion_id, symbol=symbol, error=err)
        # Not back to "approved": if the failure came after the order left, a retry would send a
        # second one. A fresh approval re-reads the position and working orders first.
        return _set(suggestion_id, "pending",
                    f"Execution failed: {err}. Check IBKR for a working order, then approve again.")


# ── Covered call ───────────────────────────────────────────────────────────────────────────

def execute_review_covered_call(suggestion_id: int) -> str:
    """Send the SELL-to-open call order for a hand-approved covered-call review card."""
    from src.core.config import get_settings
    from src.portfolio import buyer as b
    from src.portfolio.connection import (_ensure_event_loop, get_portfolio_ib, get_portfolio_lock,
                                          refresh_portfolio_pending_orders_cache)
    card = _load(suggestion_id, (CALL_ACTION,))
    if card is None:
        return "skip"
    blocked = _preflight(suggestion_id)
    if blocked:
        return blocked
    symbol, ccy = card["symbol"], card["currency"]
    if ccy != "USD":
        return _set(suggestion_id, "rejected", "Nothing sent: calls are written on US listings only.",
                    reviewed_at=datetime.utcnow())
    if not _us_options_open():
        return _set(suggestion_id, "approved", _waiting("order goes out when US options are trading"))
    if not _claim(suggestion_id):
        return "skip"

    try:
        _ensure_event_loop()
        ib = get_portfolio_ib()
        book = read_book(ib, symbol, get_settings().portfolio.ibkr_account)
        if book is None:
            return _retry_or_release(suggestion_id, "IBKR positions could not be read")
        contracts = call_size(card["quantity"], book)
        if contracts <= 0:
            log.warning("review_call_nothing_uncovered", id=suggestion_id, symbol=symbol,
                        long=book.long_shares, short_call_shares=book.short_call_shares,
                        working_sells=book.working_stock_sells + book.working_call_sells)
            return _set(suggestion_id, "rejected",
                        f"Nothing sent: no free 100-share lot. IBKR shows {book.long_shares} shares, "
                        f"{book.short_call_shares} pledged to short calls, "
                        f"{book.working_stock_sells + book.working_call_sells} already in sell orders.",
                        reviewed_at=datetime.utcnow())

        stock = broker_stock(symbol, "SMART", ccy)
        with get_portfolio_lock():
            qualified = ib.qualifyContracts(stock)
        if not qualified or not stock.conId:
            return _retry_or_release(suggestion_id, "stock contract not resolved")
        spot = _last_price(ib, stock)
        if not spot:
            return _retry_or_release(suggestion_id, "no stock price from IBKR")

        # Never below the card's strike, never closer than 5% to today's price, and — except for
        # the deep tier, whose whole point is a strike below cost that still beats selling now —
        # never below the broker's average cost. Whichever is highest.
        cost_floor = 0.0 if card.get("signal") == EXIT_CALL_DEEP_SIGNAL else book.avg_cost
        floor = max(float(card["strike"] or 0), spot * (1 + CALL_OTM_PCT), cost_floor)

        with get_portfolio_lock():
            chains = ib.reqSecDefOptParams(stock.symbol, "", "STK", stock.conId)
        chains = [c for c in (chains or []) if c.exchange == "SMART"] or list(chains or [])
        if not chains:
            return _retry_or_release(suggestion_id, "no option chain from IBKR")
        chain = chains[0]
        expiry = pick_expiry(chain.expirations, card["expiry"], date.today())
        if not expiry:
            return _set(suggestion_id, "pending", "Nothing sent: no listed expiry far enough out.")

        # The chain's strike list is the union over all expiries, so the first candidate may not
        # exist for this one: walk up, never down.
        opt = None
        for strike in sorted(s for s in chain.strikes if s >= floor - 1e-9)[:4]:
            cand = Option(stock.symbol, expiry, strike, "C", "SMART", currency=ccy)
            with get_portfolio_lock():
                ok = ib.qualifyContracts(cand)
            if ok and cand.conId:
                opt = cand
                break
        if opt is None:
            return _set(suggestion_id, "pending",
                        f"Nothing sent: no listed call at or above ${floor:.2f} for {expiry}.")

        with get_portfolio_lock():
            ticker = ib.reqMktData(opt, "", True, False)
            ib.sleep(2)
            ib.cancelMktData(opt)
        bid, ask = ticker.bid, ticker.ask
        if not bid or bid != bid or bid < MIN_CALL_BID:
            return _retry_or_release(
                suggestion_id, f"no bid of ${MIN_CALL_BID:.2f} or more for the {opt.strike:g} call {expiry}")
        # Limit AT THE BID, as the option side's covered-call writer does (wheel._write_call ->
        # orders.sell_covered_call): the bid is already on the option's price grid and the order
        # fills now instead of resting for a day and coming back for another approval.
        order_price = round(float(bid), 2)

        order = LimitOrder("SELL", contracts, order_price)
        order.tif = "DAY"
        order.outsideRth = False
        with get_portfolio_lock():
            trade = ib.placeOrder(opt, order)
        status, _filled = b._await_order_outcome(
            ib, trade, contracts, timeout=8.0, until=b._ORDER_DONE_STATES + ("Submitted", "PreSubmitted"))
        status = status or "Submitted"
        try:
            refresh_portfolio_pending_orders_cache()
        except Exception:
            pass

        ack = b._order_ack_fields(trade)
        log.info("review_call_order_placed", id=suggestion_id, symbol=symbol, contracts=contracts,
                 strike=opt.strike, expiry=expiry, price=order_price, bid=bid, ask=ask, spot=spot,
                 floor=round(floor, 2), avg_cost=round(book.avg_cost, 2), order_status=status,
                 perm_id=ack.get("perm_id"), order_id=ack.get("order_id"))
        now = datetime.utcnow()
        what = f"{contracts}× {symbol} {expiry} {opt.strike:g}C @ {order_price}"
        fields = dict(quantity=contracts, strike=float(opt.strike), expiry=expiry,
                      limit_price=order_price, reviewed_at=now)
        if status == "Filled":
            return _set(suggestion_id, "executed", f"Filled: sold {what}", **fields)
        if status in ("Cancelled", "ApiCancelled", "Inactive", "Rejected"):
            why = _order_error(trade)
            return _set(suggestion_id, "pending",
                        f"IBKR did not take the order ({status}{': ' + why if why else ''}) — nothing "
                        f"sold. Approve again to re-send.", reviewed_at=now)
        return _set(suggestion_id, "submitted",
                    f"Sent to IBKR: SELL {what}, good for today (order {ack.get('order_id')}; "
                    f"short call shares before {book.short_call_shares})", **fields)
    except Exception as e:
        err = str(e) or type(e).__name__
        log.error("review_call_execution_failed", id=suggestion_id, symbol=symbol, error=err)
        return _set(suggestion_id, "pending",
                    f"Execution failed: {err}. Check IBKR for a working order, then approve again.")


# ── After the order ────────────────────────────────────────────────────────────────────────

def reconcile_review_orders() -> int:
    """Settle review cards whose order is no longer working: filled -> executed; ended unfilled or
    partly filled -> back to "pending" for a NEW manual approval. Never re-sends anything."""
    from src.core.config import get_settings
    from src.core.suggestions import REVIEW_ONLY_ACTIONS, TradeSuggestion
    from src.portfolio.connection import _ensure_event_loop, get_portfolio_ib
    with get_db() as db:
        cards = [(s.id, s.symbol, s.action, int(s.quantity or 0), s.review_note or "", s.reviewed_at)
                 for s in db.query(TradeSuggestion).filter(
                     TradeSuggestion.status == "submitted",
                     TradeSuggestion.action.in_(REVIEW_ONLY_ACTIONS)).all()]
    if not cards:
        return 0
    _ensure_event_loop()
    ib = get_portfolio_ib()
    settled = 0
    now = datetime.utcnow()
    for sid, symbol, action, qty, note, sent_at in cards:
        if sent_at and (now - sent_at).total_seconds() < RECONCILE_GRACE_SECONDS:
            continue
        book = read_book(ib, symbol, get_settings().portfolio.ibkr_account)
        if book is None:
            continue
        is_call = action == CALL_ACTION
        if (book.working_call_sells if is_call else book.working_stock_sells) > 0:
            continue                                   # still working — leave it alone
        m = re.search(r"(?:position|short call shares) before (\d+)", note)
        if not m:
            _set(sid, "pending", "Order is no longer working at IBKR and its outcome could not be "
                                 "read — check the position, then approve again if still needed.")
            settled += 1
            continue
        before = int(m.group(1))
        done = ((book.short_call_shares - before) // 100) if is_call else (before - book.long_shares)
        unit = "contract(s)" if is_call else "shares"
        if done >= qty:
            _set(sid, "executed", f"Filled: sold {qty} {unit}", reviewed_at=now)
        elif done > 0:
            _set(sid, "pending", f"{_PARTLY}: sold {done} of {qty} {unit}, then the order ended. "
                                 f"Approve again to send the remaining {qty - done}.",
                 quantity=qty - done)
        else:
            _set(sid, "pending", "Order ended unfilled. Approve again to re-send at the current price.")
        log.info("review_order_settled", id=sid, symbol=symbol, action=action, sold=done, of=qty)
        settled += 1
    return settled


def approved_review_cards() -> list[tuple[int, str, str]]:
    """Hand-approved review cards still to be sent, oldest first, as (id, symbol, action)."""
    from src.core.suggestions import REVIEW_ONLY_ACTIONS, TradeSuggestion, is_manual_review_approval
    with get_db() as db:
        return [(s.id, s.symbol, s.action)
                for s in db.query(TradeSuggestion).filter(
                    TradeSuggestion.status == "approved",
                    TradeSuggestion.action.in_(REVIEW_ONLY_ACTIONS)).order_by(TradeSuggestion.id.asc()).all()
                if is_manual_review_approval(s.review_note)]


def run_review_orders() -> None:
    """One pass: settle ended orders, then send every hand-approved card — at most ONE per name.
    A sale and a call approved together on the same stock go a minute apart, so the second reads
    the position after the first has landed and can never commit the same shares twice."""
    reconcile_review_orders()
    seen: set[str] = set()
    for sid, symbol, action in approved_review_cards():
        if symbol in seen:
            continue
        seen.add(symbol)
        execute_review_card(sid, action)


def execute_review_card(suggestion_id: int, action: str) -> str:
    if action == CALL_ACTION:
        return execute_review_covered_call(suggestion_id)
    return execute_review_stock_sale(suggestion_id)


def review_sale_blocks(days: int = REBUY_BLOCK_DAYS) -> dict[str, str]:
    """Names the compounder must not buy, each with the reason shown on /watchlist: a hand-approved
    sale is in flight, partly done, or was filled within the last `days`. Without this the buyer
    sees the freshly opened gap to target — the biggest in the queue — and buys back what was
    just sold."""
    from src.core.suggestions import TradeSuggestion, is_manual_review_approval
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)
    out: dict[str, str] = {}
    with get_db() as db:
        for s in db.query(TradeSuggestion).filter(
                TradeSuggestion.action.in_(STOCK_ACTIONS),
                TradeSuggestion.status.in_(("approved", "executing", "submitted", "executed", "pending"))
        ).order_by(TradeSuggestion.id.asc()).all():
            note = s.review_note or ""
            if s.status in ("executing", "submitted") or (
                    s.status == "approved" and is_manual_review_approval(note)):
                out[s.symbol] = "A sale you approved on a review card is being sent or is working at the broker."
            elif s.status == "pending" and note.startswith(_PARTLY):
                out[s.symbol] = "A sale you approved on a review card is partly filled; the rest awaits your approval."
            elif s.status == "executed" and s.reviewed_at and s.reviewed_at >= cutoff:
                until = s.reviewed_at + timedelta(days=days)
                out[s.symbol] = (f"Sold on a review card you approved on {s.reviewed_at:%Y-%m-%d}. "
                                 f"Not bought back before {until:%Y-%m-%d} ({days} days).")
    return out


def review_sale_blocked_symbols(days: int = REBUY_BLOCK_DAYS) -> set[str]:
    return set(review_sale_blocks(days))
