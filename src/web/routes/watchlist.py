"""
Watchlist & Buy-Signals route — the compounder strategy's live view.

Computes the ranked universe directly from the watchlist DB rows (fundamental scores +
freshly-updated price/sma/high/momentum) via the real compounder functions, so the full
universe always shows — independent of whether a trading scan has run. Pure DB read.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.portfolio.models import PortfolioState, PortfolioWatchlist, PortfolioHolding
from src.core.suggestions import TradeSuggestion
from src.core.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


def _state(key: str, default: str = "") -> str:
    try:
        with get_db() as db:
            s = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            return s.value if s and s.value is not None else default
    except Exception:
        return default


def _num(key: str, default: float = 0.0) -> float:
    try:
        return float(_state(key) or default)
    except Exception:
        return default


# Review cards that propose parting with stock — outright, partially, or via a call that sells the
# shares if it's exercised. The ROUTE differs; the question the badge answers ("is the system asking
# to sell this?") does not. All three are in suggestions.REVIEW_ONLY_ACTIONS — they NEVER
# auto-execute, they wait for Rain to approve — so the badge means "awaiting your decision", not
# "about to happen".
_SELL_REVIEW_ACTIONS = ("sell_stock_review", "reduce_position_review", "sell_covered_call_review")
# Mirrors get_pending_suggestions() (src/core/suggestions.py) — but READ-ONLY. That helper also
# WRITES the 'expired' sweep, which must not fire on a dashboard page load, so the expiry is
# filtered here instead: an expired-but-unswept row must not show a stale SELL.
_ACTIVE_STATUSES = ("pending", "submitted", "approved", "queued")


def active_sell_map(rows, now) -> dict[str, str]:
    """symbol -> the active sell/reduce review action on it (pure; latest row wins).

    Authoritative filter — the SQL below only narrows what we load. Re-checks action and status
    here too so the badge can never outlive the rule, whatever the query returns."""
    out: dict[str, str] = {}
    for r in rows:
        action = getattr(r, "action", None)
        if action not in _SELL_REVIEW_ACTIONS:
            continue
        if getattr(r, "status", None) not in _ACTIVE_STATUSES:
            continue
        expires = getattr(r, "expires_at", None)
        if expires is not None and expires < now:
            continue                      # expired but not yet swept — no stale SELL
        sym = (getattr(r, "symbol", "") or "").upper()
        if sym:
            out[sym] = action
    return out


def _sell_reviews() -> dict[str, str]:
    """symbol -> the active sell/reduce review action on it. Empty on any error; a missing badge is
    the safe failure — it can only under-report, never invent a SELL."""
    from datetime import datetime
    try:
        with get_db() as db:
            rows = db.query(TradeSuggestion).filter(
                TradeSuggestion.action.in_(_SELL_REVIEW_ACTIONS),
                TradeSuggestion.status.in_(_ACTIVE_STATUSES),
            ).order_by(TradeSuggestion.created_at.asc()).all()
            return active_sell_map(rows, datetime.utcnow())
    except Exception as e:
        log.warning("watchlist_sell_reviews_failed", error=str(e))
        return {}


@router.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    from src.portfolio import compounder as cmp
    from src.core.config import get_settings

    pcfg = get_settings().portfolio
    cc = pcfg.compounder
    tier_alloc = {
        "breakthrough": cc.tier_breakthrough,
        "dividend": cc.tier_dividend,
        "growth": cc.tier_growth,
    }

    # Price is quoted in each name's LOCAL currency while target/holding are in the account BASE
    # currency — the table has to label both or a TSX quote reads as dollars (MFC 60.49 CAD looked
    # like a $60.49 US price next to its 43.07 NYSE twin). Resolved outside the try so the labels
    # survive a signals failure; symbol map mirrors routes/portfolio.py.
    from src.portfolio import fx as _pfx
    _rates = _pfx.load_fx_rates()
    base_ccy = _pfx.base_ccy(_rates)
    base_sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(base_ccy, "")

    signals, wl_map = [], {}
    try:
        with get_db() as db:
            rows = db.query(PortfolioWatchlist).all()
            holds = db.query(PortfolioHolding).filter(PortfolioHolding.shares > 0).all()
        wl_map = {w.symbol: w for w in rows}
        # FX-normalise holdings to the account BASE currency (investable/nlv are base) so a foreign
        # holding isn't measured against its base-ccy target unconverted (see src.portfolio.fx).
        held = {h.symbol: _pfx.to_base(h.market_value or h.total_invested or 0, h.currency, _rates)
                for h in holds}
        inv = _num("compounder_investable")
        nlv = inv / (1 - cc.cash_buffer_pct) if inv > 0 else (sum(held.values()) or 1.0)
        # compounder_reserve_unlocked_pct is stored as a PERCENT (buyer writes unlocked*100);
        # build_signals_from_watchlist wants the fraction, same as the buy path.
        signals = cmp.build_signals_from_watchlist(
            rows, held, nlv, cc, tier_alloc,
            unlocked=_num("compounder_reserve_unlocked_pct") / 100.0)
    except Exception as e:
        log.warning("watchlist_signals_failed", error=str(e))

    # Self-check: the numbers just recomputed above must match the ones the buyer actually acted
    # on. They are two implementations of one allocation, and the last time they diverged (the page
    # allocating the crash reserve, every target ~11% high) nothing noticed until Rain read a BUY
    # off a name the buyer was holding. Both sides are already in portfolio_state, so this costs one
    # read. Skipped when the snapshot is stale — a buyer that has not scanned for a day is a
    # different problem, and price drift would make the comparison meaningless.
    try:
        from datetime import datetime, timedelta
        from src.portfolio.models import PortfolioState
        with get_db() as db:
            _snap_row = db.query(PortfolioState).filter(
                PortfolioState.key == "compounder_signals").first()
            _snap_json, _snap_at = ((_snap_row.value, _snap_row.updated_at) if _snap_row
                                    else (None, None))
        if signals and _snap_json and _snap_at and _snap_at > datetime.utcnow() - timedelta(hours=24):
            import json as _json
            _drift = cmp.signal_parity_drift(signals, _json.loads(_snap_json))
            if _drift:
                log.warning("compounder_signal_parity_drift",
                            note="the /watchlist allocation no longer matches the buyer's — "
                                 "build_signals_from_watchlist has drifted from run_compounder",
                            **_drift)
    except Exception as e:                 # never let the self-check break the page
        log.warning("compounder_signal_parity_check_failed", error=str(e))

    tier_summary: dict[str, dict] = {}
    for s in signals:
        d = tier_summary.setdefault(s.get("tier", "growth"), {"count": 0, "target": 0.0, "deployed": 0.0})
        d["count"] += 1
        d["target"] += s.get("target", 0) or 0
        d["deployed"] += s.get("current", 0) or 0

    reserve = {
        "drawdown_pct": _num("compounder_drawdown_pct"),
        "tranches_fired": int(_num("compounder_tranches_fired")),
        "unlocked_pct": _num("compounder_reserve_unlocked_pct"),
        "investable": _num("compounder_investable"),
        "live_target": _num("compounder_live_target"),
        "deployed": _num("compounder_deployed"),
        "daily_budget": _num("compounder_daily_budget"),
        "reserve_peak": _num("compounder_reserve_peak"),
    }
    strategy = _state("strategy") or "classic"

    # Buy-queue heads as of the last executed scan (written by the buyer, not recomputed here — the
    # stars reflect what the engine actually decided, not a page load). `next_buy_now` is the head of
    # the slice that could trade that scan (solid star); `next_buy` is the head of the FULL ranked
    # universe (hollow star), which differs when the leading name's own exchange is shut.
    next_buy = (_state("compounder_next_buy") or "").strip().upper()
    next_buy_now = (_state("compounder_next_buy_now") or "").strip().upper()

    # Active sell/reduce review cards → a SELL badge beside the ticker, mirroring the buy stars.
    sell_reviews = _sell_reviews()

    slots_allowed = sum(1 for s in signals if (s.get("target") or 0) > 0)
    slots_filled = sum(1 for s in signals if (s.get("target") or 0) > 0 and (s.get("current") or 0) > 0)

    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "signals": signals,
        "wl_map": wl_map,
        "next_buy": next_buy,
        "next_buy_now": next_buy_now,
        "sell_reviews": sell_reviews,
        "base_ccy": base_ccy,
        "base_sym": base_sym,
        "tier_summary": tier_summary,
        "reserve": reserve,
        "strategy": strategy,
        "is_compounder": strategy == "compounder",
        "slots_filled": slots_filled,
        "slots_allowed": slots_allowed,
    })
