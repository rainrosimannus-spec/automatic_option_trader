"""Phase 2 (2026-11-04): cash-settled INDEX PUT sleeve — short XSP puts beside the wheel.

Why this exists: the single-name 0-3 DTE wheel finished ITM 33% of the time on names that
move 7-10% in a day and then held the broken stock. SPY breached a 3-day 2.5% put 1% of
the time this year, and a cash-settled European index put (XSP = SPX/10, $100 multiplier,
CBOE, PM-settled) can never become a stock lot. MarsWalk: wheel + sleeve (k=2.0, 1 DTE,
30% of NLV as margin) ≈ 24%/yr over the 6-year replay at 8.8% max DD with deliberately
flat-VIX pricing; sleeve alone 13.5%/yr. Mirrors src/marswalk/engine.py Params.index_put_*.

Lifecycle (own position type `index_put`, never touched by the wheel):
  scan   — at the configured ET times: gates (enabled, VIX, circuit breaker, intraday loss,
           margin usage, buying power, ladder count), spot, σ, strike = spot × (1 − max(floor,
           k·σ·√DTE)) on the 1-point grid, live quote, whatIf margin → contracts, then either
           log the would-be order (dry_run) or sell_index_put + record Trade + Position.
  manage — every 5 min in RTH: stop (ask ≥ stop_mult × credit → buy back) and settlement of
           yesterday's expiries at the index close (realized = premium − intrinsic − fees).
Shares cash and the IBKR margin gates with the wheel; nothing else.
"""
from __future__ import annotations

import math
from datetime import datetime, date, timedelta
from typing import Optional

from src.core.config import get_settings
from src.core.database import get_db
from src.core.logger import get_logger
from src.core.models import Position, Trade, PositionStatus, TradeType, OrderStatus

log = get_logger(__name__)

POSITION_TYPE = "index_put"
INDEX_SYMBOLS = ("XSP", "SPX", "SPXW", "NDX", "XND", "RUT")


# ── pure helpers (unit-tested; same arithmetic as the MarsWalk sleeve) ──────────────────
def daily_sigma(closes: list[float], lookback: int = 20) -> Optional[float]:
    """σ of daily log returns over the last `lookback` closes. None on short history."""
    if not closes or len(closes) < 6:
        return None
    win = closes[-(lookback + 1):]
    rets = [math.log(win[i] / win[i - 1]) for i in range(1, len(win)) if win[i - 1] > 0 and win[i] > 0]
    if len(rets) < 5:
        return None
    mu = sum(rets) / len(rets)
    return (sum((r - mu) ** 2 for r in rets) / len(rets)) ** 0.5


def index_put_strike(spot: float, sigma: float, *, dte: int, sigma_k: float, otm_floor: float,
                     grid: float = 1.0) -> float:
    """spot × (1 − max(otm_floor, sigma_k × σ × √DTE)), rounded DOWN to the strike grid."""
    otm = max(otm_floor, sigma_k * sigma * math.sqrt(max(dte, 1)))
    raw = spot * (1.0 - otm)
    return math.floor(raw / grid) * grid


def margin_proxy_per_contract(spot: float, strike: float, margin_pct: float = 0.15) -> float:
    """SPAN-like proxy used only when IBKR whatIf is unavailable."""
    return max(margin_pct * spot - (spot - strike), 0.10 * strike) * 100.0


def contracts_for_budget(nlv: float, budget_pct: float, margin_per_contract: float,
                         margin_in_use: float, max_contracts: int) -> int:
    """How many contracts the remaining sleeve budget (budget_pct × NLV − margin already
    used by open sleeve puts) affords at `margin_per_contract`."""
    if nlv <= 0 or margin_per_contract <= 0:
        return 0
    free = budget_pct * nlv - margin_in_use
    return int(max(0, min(max_contracts, math.floor(free / margin_per_contract))))


def settlement_pnl(premium: float, strike: float, settle_px: float, qty: int,
                   commission_total: float = 0.0) -> tuple[float, float]:
    """(intrinsic paid, realized P&L) for a cash-settled short put at `settle_px`."""
    intrinsic = max(0.0, strike - settle_px) * 100.0 * qty
    realized = premium * 100.0 * qty - intrinsic - commission_total
    return round(intrinsic, 2), round(realized, 2)


def stop_triggered(ask: Optional[float], credit: float, stop_mult: float) -> bool:
    if not ask or ask <= 0 or credit <= 0 or stop_mult <= 0:
        return False
    return ask >= stop_mult * credit


def pick_expiry(expirations: list[str], today: date, dte: int) -> Optional[str]:
    """The expiry `dte` trading days out (Mon-Fri; holidays ignored — the chain only lists
    real expiries, so the next listed one at/after the target date is taken)."""
    target = today
    steps = 0
    while steps < dte:
        target += timedelta(days=1)
        if target.weekday() < 5:
            steps += 1
    tgt = target.strftime("%Y%m%d")
    for e in expirations:
        if e >= tgt:
            return e
    return None


# ── the live seller ─────────────────────────────────────────────────────────────────────
class IndexPutSeller:
    def __init__(self, risk=None):
        self.cfg = get_settings().index_puts
        self.risk = risk

    # -- state helpers
    def _open_positions(self, db) -> list[Position]:
        return db.query(Position).filter(
            Position.status == PositionStatus.OPEN,
            Position.position_type == POSITION_TYPE,
        ).all()

    def _nlv(self) -> Optional[float]:
        try:
            from src.broker.account import get_account_summary
            summ = get_account_summary()
            v = summ.get("net_liquidation") if isinstance(summ, dict) else getattr(summ, "net_liquidation", None)
            return float(v) if v else None
        except Exception as e:
            log.warning("index_put_nlv_unavailable", error=str(e))
            return None

    def _gates(self) -> tuple[bool, str]:
        """Reuse the wheel's account-level gates; none of the per-name ones."""
        if not self.cfg.enabled:
            return False, "disabled"
        if self.risk is None:
            return True, ""
        for name in ("check_vix_gate", "check_daily_circuit_breaker", "check_intraday_loss",
                     "check_margin_usage", "check_buying_power"):
            fn = getattr(self.risk, name, None)
            if fn is None:
                continue
            try:
                chk = fn()
            except Exception as e:
                log.warning("index_put_gate_error", gate=name, error=str(e))
                continue
            if not chk.allowed:
                return False, f"{name}: {chk.reason}"
        try:
            vix = self.risk.get_regime().vix if hasattr(self.risk, "get_regime") else None
        except Exception:
            vix = None
        if vix is not None and vix > self.cfg.vix_max:
            return False, f"VIX {vix:.1f} > {self.cfg.vix_max}"
        return True, ""

    # -- scan
    def scan(self) -> dict | None:
        """One scan: returns the decision dict (also logged), or None when nothing to do."""
        ok, why = self._gates()
        if not ok:
            log.info("index_put_scan_blocked", reason=why)
            return None
        from src.broker.market_data import (get_index_spot, get_index_option_params,
                                            get_index_option_quote)
        from src.broker.orders import get_whatif_margin_index, sell_index_put
        from src.portfolio.fmp import get_price_history

        with get_db() as db:
            open_puts = self._open_positions(db)
            if len(open_puts) >= self.cfg.max_open:
                log.info("index_put_scan_ladder_full", open=len(open_puts), max_open=self.cfg.max_open)
                return None

            spot = get_index_spot(self.cfg.symbol, self.cfg.exchange)
            if not spot:
                log.warning("index_put_no_spot", symbol=self.cfg.symbol)
                return None
            closes = get_price_history("SPY", int(self.cfg.sigma_lookback * 1.6) + 15) or []
            sigma = daily_sigma(closes, self.cfg.sigma_lookback)
            if sigma is None:
                log.warning("index_put_no_sigma", bars=len(closes))
                return None
            exps, strikes, _cid = get_index_option_params(self.cfg.symbol, self.cfg.exchange)
            expiry = pick_expiry(exps, date.today(), self.cfg.dte)
            if not expiry:
                log.warning("index_put_no_expiry", have=len(exps))
                return None
            strike = index_put_strike(spot, sigma, dte=self.cfg.dte, sigma_k=self.cfg.sigma_k,
                                      otm_floor=self.cfg.otm_floor)
            if strikes:
                below = [k for k in strikes if k <= strike]
                if below:
                    strike = max(below)
            bid, ask, con_id = get_index_option_quote(self.cfg.symbol, expiry, strike, "P",
                                                      self.cfg.exchange, self.cfg.trading_class)
            if not bid or bid < self.cfg.min_bid:
                log.info("index_put_bid_too_small", strike=strike, expiry=expiry, bid=bid)
                return None
            nlv = self._nlv()
            if not nlv:
                return None
            m1 = get_whatif_margin_index(self.cfg.symbol, expiry, strike, 1, bid,
                                         self.cfg.exchange, self.cfg.trading_class)
            margin_pc = m1 if m1 else margin_proxy_per_contract(spot, strike, self.cfg.margin_pct_fallback)
            in_use = sum(margin_proxy_per_contract(spot, p.strike, self.cfg.margin_pct_fallback) * p.quantity
                         for p in open_puts)
            qty = contracts_for_budget(nlv, self.cfg.budget_pct, margin_pc, in_use, self.cfg.max_contracts)
            decision = {
                "symbol": self.cfg.symbol, "expiry": expiry, "strike": strike, "spot": round(spot, 2),
                "sigma_daily": round(sigma, 5), "otm_pct": round((1 - strike / spot) * 100, 2),
                "bid": bid, "ask": ask, "margin_per_contract": round(margin_pc, 0),
                "margin_source": "whatif" if m1 else "proxy", "nlv": round(nlv, 0),
                "qty": qty, "dry_run": self.cfg.dry_run,
            }
            if qty <= 0:
                log.info("index_put_no_budget", **decision)
                return decision
            if self.cfg.dry_run:
                log.info("index_put_DRY_RUN_would_sell", **decision)
                return decision

            trade = sell_index_put(self.cfg.symbol, expiry, strike, qty, bid,
                                   self.cfg.exchange, self.cfg.trading_class, con_id)
            if not trade:
                return decision
            db.add(Trade(position_id=None, symbol=self.cfg.symbol, trade_type=TradeType.SELL_PUT,
                         strike=strike, expiry=expiry, premium=bid, quantity=qty, fill_price=bid,
                         commission=0.0, order_id=trade.order.orderId, order_status=OrderStatus.SUBMITTED,
                         bid_at_entry=bid, ask_at_entry=ask, mid_at_entry=(bid + ask) / 2 if ask else bid,
                         notes=f"index_put sleeve: spot {spot:.2f} σ {sigma:.4f} otm {decision['otm_pct']}%"))
            db.add(Position(symbol=self.cfg.symbol, status=PositionStatus.OPEN, position_type=POSITION_TYPE,
                            strike=strike, expiry=expiry, entry_premium=bid, cost_basis=None,
                            wheel_exit_mode=False, quantity=qty, total_premium_collected=bid * 100 * qty,
                            realized_pnl=0.0, unrealized_pnl=0.0, is_wheel=False, opened_at=datetime.utcnow()))
            db.commit()
            log.info("index_put_sold", order_id=trade.order.orderId, **decision)
            return decision

    # -- manage: stop + settlement
    def manage(self) -> list[str]:
        if not self.cfg.enabled:
            return []
        from src.broker.market_data import get_index_option_quote, get_index_spot
        from src.broker.orders import buy_to_close_index_put
        from src.portfolio.fmp import get_price_history
        acted: list[str] = []
        today = date.today().strftime("%Y%m%d")
        with get_db() as db:
            rows = db.query(Position).filter(
                Position.position_type == POSITION_TYPE,
                Position.status.in_([PositionStatus.OPEN, PositionStatus.EXPIRED]),
            ).all()
            for pos in rows:
                # settlement: expiry has passed and no settlement trade booked yet
                if pos.expiry and pos.expiry < today:
                    settled = db.query(Trade).filter(
                        Trade.symbol == pos.symbol, Trade.strike == pos.strike, Trade.expiry == pos.expiry,
                        Trade.trade_type == TradeType.BUY_PUT, Trade.notes.like("index_put settlement%"),
                    ).first()
                    if settled:
                        continue
                    settle_px = self._settlement_price(pos.expiry, get_price_history)
                    if settle_px is None:
                        log.warning("index_put_settlement_price_missing", symbol=pos.symbol, expiry=pos.expiry)
                        continue
                    intrinsic, realized = settlement_pnl(pos.entry_premium or 0.0, pos.strike or 0.0,
                                                         settle_px, pos.quantity)
                    db.add(Trade(position_id=pos.id, symbol=pos.symbol, trade_type=TradeType.BUY_PUT,
                                 strike=pos.strike, expiry=pos.expiry, premium=intrinsic / (100 * max(pos.quantity, 1)),
                                 quantity=pos.quantity, fill_price=intrinsic / (100 * max(pos.quantity, 1)),
                                 commission=0.0, order_status=OrderStatus.FILLED,
                                 notes=f"index_put settlement at {settle_px:.2f} (intrinsic ${intrinsic:,.0f})"))
                    pos.status = PositionStatus.CLOSED if intrinsic > 0 else PositionStatus.EXPIRED
                    pos.closed_at = datetime.utcnow()
                    pos.realized_pnl = realized
                    db.commit()
                    log.info("index_put_settled", symbol=pos.symbol, strike=pos.strike, expiry=pos.expiry,
                             settle=settle_px, intrinsic=intrinsic, realized=realized)
                    acted.append(f"settle {pos.strike}")
                    continue
                if pos.status != PositionStatus.OPEN or self.cfg.stop_mult <= 0:
                    continue
                _bid, ask, _cid = get_index_option_quote(pos.symbol, pos.expiry, pos.strike, "P",
                                                         self.cfg.exchange, self.cfg.trading_class)
                if stop_triggered(ask, pos.entry_premium or 0.0, self.cfg.stop_mult):
                    limit = round(ask * 1.02 + 0.05, 2)
                    log.warning("index_put_stop_triggered", symbol=pos.symbol, strike=pos.strike,
                                expiry=pos.expiry, credit=pos.entry_premium, ask=ask, limit=limit)
                    if self.cfg.dry_run:
                        continue
                    trade = buy_to_close_index_put(pos.symbol, pos.expiry, pos.strike, pos.quantity, limit,
                                                   self.cfg.exchange, self.cfg.trading_class)
                    if trade:
                        db.add(Trade(position_id=pos.id, symbol=pos.symbol, trade_type=TradeType.BUY_PUT,
                                     strike=pos.strike, expiry=pos.expiry, premium=limit, quantity=pos.quantity,
                                     fill_price=limit, commission=0.0, order_id=trade.order.orderId,
                                     order_status=OrderStatus.SUBMITTED,
                                     notes=f"index_put stop at {self.cfg.stop_mult}x credit"))
                        db.commit()
                        acted.append(f"stop {pos.strike}")
        return acted

    def _settlement_price(self, expiry: str, get_price_history) -> Optional[float]:
        """XSP settles on the SPX close of expiry day. Proxy: live XSP/SPX spot is wrong after
        the fact, so use the SPY close of that day scaled by the latest XSP/SPY ratio."""
        from src.broker.market_data import get_index_spot
        closes = get_price_history("SPY", 15) or []
        if not closes:
            return None
        spot_xsp = get_index_spot(self.cfg.symbol, self.cfg.exchange)
        spy_last = closes[-1]
        ratio = (spot_xsp / spy_last) if (spot_xsp and spy_last) else 1.0
        # expiry-day close = the close `n` days back where n = trading days since expiry
        exp_d = datetime.strptime(expiry, "%Y%m%d").date()
        n = 0
        d = date.today()
        while d > exp_d:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                n += 1
        if n >= len(closes):
            return None
        return round(closes[-1 - n] * ratio, 2)
