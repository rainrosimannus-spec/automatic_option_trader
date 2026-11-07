"""Per-sleeve P&L for the options account — the number Rain reads instead of my arguments.

Four sleeves, all from the account's own fills in options.db (FILLED trades + positions):
  single_puts    — single-name short puts: premium received − bought back − commissions
  covered_calls  — calls written on assigned stock: premium − buybacks − commissions
  stock          — assigned lots: realised cash on closed lots (sale − assignment strike)
                   plus IBKR's unrealised on open lots
  index_puts     — the XSP sleeve: premium − settlement/buybacks − commissions (own symbol set)
Each is given for the whole life of the account and for the trailing 30 days (realised cash
only in the window; open-lot unrealised is reported once, as of now). A total row sums them.
Pure over a SQLAlchemy session so the dashboard, /api/sleeve-pnl and tests share one source.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.core.models import Position, PositionStatus, Trade, TradeType, OrderStatus

INDEX_SYMBOLS = ("XSP", "SPX", "SPXW", "NDX", "XND", "RUT")
HEDGE_SYMBOLS = ("SPY",)   # the tail hedge buys SPY puts (hedge.py); nothing else trades SPY options here
_PREMIUM_IN = (TradeType.SELL_PUT, TradeType.SELL_CALL)
_PREMIUM_OUT = (TradeType.BUY_PUT, TradeType.BUY_CALL)
_STOCK_IN = (TradeType.SELL_STOCK, TradeType.CALLED_AWAY)
_STOCK_OUT = (TradeType.BUY_STOCK, TradeType.ASSIGNMENT)


def _option_cash(trades) -> float:
    cash = 0.0
    for t in trades:
        q = (t.quantity or 0) * 100 * (t.premium or 0.0)
        if t.trade_type in _PREMIUM_IN:
            cash += q
        elif t.trade_type in _PREMIUM_OUT:
            cash -= q
        cash -= (t.commission or 0.0)
    return cash


def compute_sleeve_pnl(db, now: datetime | None = None, window_days: int = 30) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(days=window_days)
    trades = db.query(Trade).filter(Trade.order_status == OrderStatus.FILLED).all()
    trades = [t for t in trades if t.symbol not in ("XEON", "XFFE")]      # cash-parking ETFs are not a sleeve
    is_index = lambda t: t.symbol in INDEX_SYMBOLS  # noqa: E731
    is_hedge = lambda t: t.symbol in HEDGE_SYMBOLS and t.trade_type in (TradeType.BUY_PUT, TradeType.SELL_PUT)  # noqa: E731
    puts_all = [t for t in trades if t.trade_type in (TradeType.SELL_PUT, TradeType.BUY_PUT) and not is_index(t) and not is_hedge(t)]
    calls_all = [t for t in trades if t.trade_type in (TradeType.SELL_CALL, TradeType.BUY_CALL) and not is_index(t)]
    idx_all = [t for t in trades if is_index(t)]
    hedge_all = [t for t in trades if is_hedge(t)]

    def win(ts):
        return [t for t in ts if t.created_at and t.created_at >= since]

    # Stock sleeve from the Position ledger (one row per lot; realized_pnl = sale − assignment
    # strike per share, verified on the 2026-11-04 exits; unrealized_pnl = IBKR's own mark on
    # open lots). Trade cash flows are NOT used here: assignment rows carry contracts and the
    # synced delivery rows carry shares, and mixing them double-counts.
    open_lots = db.query(Position).filter(
        Position.status == PositionStatus.OPEN, Position.position_type == "stock").all()
    closed_lots = db.query(Position).filter(
        Position.status == PositionStatus.CLOSED, Position.position_type == "stock").all()
    open_cost = sum((p.quantity or 0) * (p.cost_basis or 0.0) for p in open_lots)
    open_unreal = sum((p.unrealized_pnl or 0.0) for p in open_lots)
    stock_realised_all = sum((p.realized_pnl or 0.0) for p in closed_lots)
    stock_realised_30 = sum((p.realized_pnl or 0.0) for p in closed_lots
                            if p.closed_at and p.closed_at >= since)

    rows = {
        "single_puts": {"all": _option_cash(puts_all), "d30": _option_cash(win(puts_all)),
                        "n_all": sum(1 for t in puts_all if t.trade_type == TradeType.SELL_PUT),
                        "n_30": sum(1 for t in win(puts_all) if t.trade_type == TradeType.SELL_PUT)},
        "covered_calls": {"all": _option_cash(calls_all), "d30": _option_cash(win(calls_all)),
                          "n_all": sum(1 for t in calls_all if t.trade_type == TradeType.SELL_CALL),
                          "n_30": sum(1 for t in win(calls_all) if t.trade_type == TradeType.SELL_CALL)},
        "stock": {"all": stock_realised_all, "d30": stock_realised_30, "open_unrealised": open_unreal,
                  "open_lots": len(open_lots), "open_cost": open_cost},
        "index_puts": {"all": _option_cash(idx_all), "d30": _option_cash(win(idx_all)),
                       "n_all": sum(1 for t in idx_all if t.trade_type == TradeType.SELL_PUT),
                       "n_30": sum(1 for t in win(idx_all) if t.trade_type == TradeType.SELL_PUT)},
        # tail hedge: long SPY puts, so BUY_PUT is the cost and SELL_PUT / settlement the proceeds —
        # _option_cash already signs them that way (sells +, buys −)
        "tail_hedge": {"all": _option_cash(hedge_all), "d30": _option_cash(win(hedge_all)),
                       "n_all": sum(1 for t in hedge_all if t.trade_type == TradeType.BUY_PUT),
                       "n_30": sum(1 for t in win(hedge_all) if t.trade_type == TradeType.BUY_PUT)},
    }
    total_all = sum(r["all"] for r in rows.values()) + open_unreal
    total_30 = sum(r["d30"] for r in rows.values())
    fees_all = sum((t.commission or 0.0) for t in trades)
    fees_all += sum((t.commission or 0.0) for t in db.query(Trade).filter(
        Trade.order_status == OrderStatus.FILLED, Trade.trade_type.in_(_STOCK_IN + _STOCK_OUT)).all())
    return {"rows": rows, "total_all": total_all, "total_30": total_30, "fees_all": fees_all,
            "window_days": window_days, "as_of": now.strftime("%Y-%m-%d %H:%M UTC")}
