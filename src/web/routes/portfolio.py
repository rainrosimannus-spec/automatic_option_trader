"""
Portfolio dashboard route — track long-term holdings, buy signals, and performance.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.portfolio.models import (
    PortfolioHolding, PortfolioTransaction, PortfolioWatchlist, PortfolioState,
    PortfolioPutEntry,
)
from src.core.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


def _get_state(key: str) -> str:
    try:
        with get_db() as db:
            s = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            return s.value if s else ""
    except Exception:
        return ""


def _build_portfolio_performance() -> dict:
    """
    Graph starts today at 0% and grows forward from there.
    Formula: (NLV / total_invested_usd - 1) * 100, anchored to first point = 0%.
    BRK-B benchmark anchored the same way.
    """
    from src.core.models import AccountSnapshot, SystemState
    from src.portfolio.capital_injections import get_total_invested_base
    from src.core.config import get_settings
    from datetime import date as date_type

    _pacct = getattr(get_settings().portfolio, "ibkr_account", "") or None
    # base-currency (EUR) invested so the return % matches the EUR NLV in the snapshots
    total_invested_usd = get_total_invested_base(account_id=_pacct)
    today_str = str(date_type.today())

    with get_db() as db:
        # Store today as graph start date if not already set
        start_row = db.query(SystemState).filter(
            SystemState.key == "graph_start_date"
        ).first()
        if not start_row:
            db.add(SystemState(key="graph_start_date", value=today_str))
            db.commit()
            start_date = today_str
        else:
            start_date = start_row.value

        snapshots = (
            db.query(AccountSnapshot)
            .filter(AccountSnapshot.date >= start_date)
            .order_by(AccountSnapshot.date.asc())
            .all()
        )

    # Per-date invested base: divide each snapshot's NLV by the capital that was actually in the
    # account AS OF that date (cumulative net deposits), not by a single current total. Otherwise a
    # mid-series deposit makes every older (smaller-NLV) snapshot divide by the new, larger invested
    # base — dragging the anchor down so the deposit shows up as fake growth (e.g. a 10k→20k deposit
    # reading as +50%). With the per-date base, a deposit raises NLV and the invested base together
    # and the return dilutes to ~flat. Ledger empty / dates before the first deposit → fall back to
    # the latest scalar so behaviour is unchanged until the deposit ledger is populated.
    from src.portfolio.capital_injections import get_capital_ledger_base
    from datetime import date as _date
    ledger = get_capital_ledger_base(account_id=_pacct)
    _DEPOSIT_GRACE_DAYS = 7   # settlement/value-date slack: Flex stamps a deposit a day or two after
                              # the cash actually lands in NLV; without slack the deposit reads as a
                              # one-day return spike (NLV up, base not yet — the 6/23 +100% artifact).

    def _invested_asof(day: str, nlv: float) -> float:
        """Cumulative net deposits in effect on `day`, recognised into the invested base only when
        this day's NLV actually reflects the cash — in BOTH directions:
          • a deposit dated up to _DEPOSIT_GRACE_DAYS in the FUTURE is pulled in early once NLV
            already holds it (settle-date lag);
          • a deposit dated on/just before `day` is HELD BACK while it is still inside the settlement
            window AND this day's NLV has not yet caught up — e.g. a weekend deposit measured against a
            Friday-frozen snapshot. Without this a €2M Saturday deposit divided a stale Friday NLV and
            printed a fake −30/−47% one-weekend dip.
        Deposits OLDER than the grace window are always counted, so a genuine later drawdown (NLV
        legitimately below invested) still shows in full and is never clipped."""
        base = 0.0
        for d, cum in ledger:
            try:
                if d <= day:
                    gap_past = (_date.fromisoformat(day) - _date.fromisoformat(d)).days
                    if gap_past > _DEPOSIT_GRACE_DAYS or nlv >= cum * 0.98:
                        base = cum      # settled long ago, or NLV already reflects this deposit
                        continue
                    break               # recent deposit not yet in this day's NLV → hold the base
                gap = (_date.fromisoformat(d) - _date.fromisoformat(day)).days
            except Exception:
                break
            if 0 < gap <= _DEPOSIT_GRACE_DAYS and nlv >= cum * 0.98:
                base = cum          # NLV already holds this near-future deposit → recognise it now
            else:
                break
        return base

    labels = []
    raw_returns = []
    for snap in snapshots:
        nlv = snap.portfolio_nlv if snap.portfolio_nlv and snap.portfolio_nlv > 0 else None
        if not nlv or nlv <= 0:
            continue
        day = str(snap.date)[:10]
        invested = _invested_asof(day, nlv) or total_invested_usd
        if not invested or invested <= 0:
            continue
        labels.append(day)
        raw_returns.append((nlv / invested - 1.0) * 100.0)

    if not raw_returns:
        return {
            "labels": [today_str],
            "portfolio_data": [0.0],
            "brkb_data": [0.0],
            "total_invested_usd": total_invested_usd,
            "current_return_pct": 0.0,
        }

    # Anchor first point to 0% — graph grows forward from today
    anchor = raw_returns[0]
    portfolio_data = [round(r - anchor, 4) for r in raw_returns]
    current_return_pct = round(raw_returns[-1], 2)

    brkb_data = []  # loaded async via /portfolio/brkb-data

    return {
        "labels": labels,
        "portfolio_data": portfolio_data,
        "brkb_data": brkb_data,
        "total_invested_usd": total_invested_usd,
        "current_return_pct": current_return_pct,
    }






@router.get("/portfolio/brkb-data")
async def brkb_data_endpoint(request: Request):
    """Return BRK-B benchmark series as JSON for async chart loading."""
    from fastapi.responses import JSONResponse
    from src.portfolio.connection import get_cached_portfolio_account
    import json, os
    # Read from cache file directly (populated hourly by scheduler)
    try:
        cache_file = "data/portfolio_account_cache.json"
        with open(cache_file) as f:
            cache = json.load(f)
        brkb_history = cache.get("brkb_history", {})
    except Exception:
        brkb_history = {}
    perf = _build_portfolio_performance()
    labels = perf.get("labels", [])
    if brkb_history and labels:
        sorted_dates = sorted(brkb_history.keys())
        prices = []
        for label in labels:
            p = brkb_history.get(label)
            if p is None:
                for d in reversed(sorted_dates):
                    if d <= label:
                        p = brkb_history[d]
                        break
            prices.append(p)
        if prices and prices[0]:
            anchor = prices[0]
            brkb = [round((p / anchor - 1.0) * 100.0, 4) if p else None for p in prices]
        else:
            brkb = []
    else:
        brkb = []
    return JSONResponse({"labels": labels, "brkb": brkb})

# FX rate cache — refreshed once per page load
_fx_cache: dict = {}
_fx_cache_time: float = 0.0
_FX_CACHE_TTL = 300  # seconds

def _get_fx_rates(currencies: list) -> dict:
    """Get FX rates from IBKR cache file (populated hourly by scheduler)."""
    import json
    global _fx_cache, _fx_cache_time
    import time
    now = time.time()
    if _fx_cache and (now - _fx_cache_time) < _FX_CACHE_TTL:
        return _fx_cache
    try:
        with open("data/portfolio_account_cache.json") as f:
            cache = json.load(f)
        rates = cache.get("fx_rates", {})
        if rates:
            _fx_cache = rates
            _fx_cache_time = now
        return rates
    except Exception:
        return _fx_cache

def _to_usd(amount: float, currency: str, fx_rates: dict = None) -> float:
    """Convert an amount in the given currency to USD."""
    if not amount or currency in ("USD", None):
        return amount or 0.0
    rates = fx_rates if fx_rates is not None else _fx_cache
    rate = rates.get(currency)
    if rate:
        return amount * rate
    return amount


def _portfolio_base_ccy(fx_rates: dict = None) -> str:
    """Account BASE currency = the one IBKR reports with ExchangeRate == 1.0 (EUR for U26413485).
    Falls back to USD if the cache has no rates yet."""
    rates = fx_rates if fx_rates is not None else _fx_cache
    for c, r in (rates or {}).items():
        try:
            if abs(float(r) - 1.0) < 1e-9:
                return c
        except Exception:
            pass
    return "USD"


def _to_base(amount: float, currency: str, fx_rates: dict = None, base_ccy: str = None) -> float:
    """Convert an amount in `currency` to the account BASE currency using IBKR's per-currency
    ExchangeRate (which is quoted currency→base). Unlike _to_usd, this does NOT pass USD through —
    so for a euro-base account a USD holding is converted to EUR."""
    if not amount:
        return 0.0
    rates = fx_rates if fx_rates is not None else _fx_cache
    base = base_ccy or _portfolio_base_ccy(rates)
    if currency in (base, "BASE", None):
        return amount
    rate = (rates or {}).get(currency)
    return amount * rate if rate else amount

def _build_tier_breakdown(holdings, fx_rates=None) -> dict:
    """Build tier allocation data for pie/bar chart. The cash-yield ETF (the parked cash reserve, e.g.
    XEON, which carries tier='growth') is bucketed as 'cash' — it's not a compounder tier holding, so it
    must not inflate the Growth slice."""
    from src.core.config import get_settings
    _park = getattr(get_settings().portfolio, "cash_yield_symbol", None)
    tiers = {"dividend": 0, "growth": 0, "breakthrough": 0, "cash": 0}
    for h in holdings:
        if _park and h.symbol == _park:
            tiers["cash"] += _to_usd(h.market_value or 0, h.currency, fx_rates)
            continue
        tier = h.tier or "growth"
        tiers[tier] = tiers.get(tier, 0) + _to_usd(h.market_value or 0, h.currency, fx_rates)
    return tiers


def _build_top_performers(holdings) -> list[dict]:
    """Top and bottom performers by unrealized P&L %."""
    performers = []
    for h in holdings:
        if h.total_invested and h.total_invested > 0:
            pnl_pct = ((h.market_value or 0) - h.total_invested) / h.total_invested * 100
            performers.append({
                "symbol": h.symbol,
                "name": h.name or "",
                "pnl_pct": pnl_pct,
                "pnl_abs": (h.market_value or 0) - h.total_invested,
                "tier": h.tier or "growth",
            })
    performers.sort(key=lambda x: x["pnl_pct"], reverse=True)
    return performers


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    """Main portfolio dashboard."""
    with get_db() as db:
        holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.shares > 0
        ).order_by(PortfolioHolding.market_value.desc()).all()

        watchlist = db.query(PortfolioWatchlist).order_by(
            PortfolioWatchlist.buy_signal.desc(),
            PortfolioWatchlist.composite_score.desc(),
            PortfolioWatchlist.compound_quality_pct.desc(),
        ).all()

        transactions = db.query(PortfolioTransaction).order_by(
            PortfolioTransaction.created_at.desc()
        ).limit(20).all()

        put_entries = db.query(PortfolioPutEntry).filter(
            PortfolioPutEntry.status == "open"
        ).order_by(PortfolioPutEntry.expiry.asc()).all()

    # FX conversion: convert non-USD holdings to USD for display


    # Total invested = net capital deposited (deposits − withdrawals, from injections
    # table scoped to the portfolio account; not cost basis)
    from src.portfolio.capital_injections import get_total_invested_base
    from src.core.config import get_settings
    # Pre-fetch all FX rates in one API call
    currencies = list(set(h.currency for h in holdings if h.currency not in ("USD", None)))
    fx_rates = _get_fx_rates(currencies)
    _pacct = getattr(get_settings().portfolio, "ibkr_account", "") or None
    # Total Invested + Total Value reported in the account BASE currency (EUR for U26413485) so the
    # top cards match the IBKR EUR figures — not a USD mix. Per-stock rows are left in native USD.
    _base_ccy = _portfolio_base_ccy(fx_rates)
    total_invested = get_total_invested_base(account_id=_pacct)
    total_value = sum(_to_base(h.market_value or 0, h.currency, fx_rates, _base_ccy) for h in holdings)

    # Parked-cash card (top of page): idle cash swept into the money-market ETF (XEON) for ~€STR yield.
    # Amount is the ETF holding's market value in BASE ccy (0 when nothing is parked); the annual % is the
    # configured displayed rate. Reads the holdings already loaded — no IBKR call.
    _pcfg = get_settings().portfolio
    _park_symbol = getattr(_pcfg, "cash_yield_symbol", "XEON")
    _park_annual_pct = float(getattr(_pcfg, "cash_yield_annual_pct", 0.0) or 0.0)
    _park_h = next((h for h in holdings if h.symbol == _park_symbol), None)
    parked_amount = _to_base(_park_h.market_value or 0, _park_h.currency, fx_rates, _base_ccy) if _park_h else 0.0
    parked_base_sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(_base_ccy, "")
    ibkr_unrealized_pnl = None
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        _acct = get_cached_portfolio_account()
        ibkr_unrealized_pnl = _acct.get("unrealized_pnl")
    except Exception:
        pass

    if ibkr_unrealized_pnl is not None:
        total_pnl = ibkr_unrealized_pnl
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    else:
        total_pnl = total_value - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    # Dividend income — from IBKR cache (populated by Flex Query)
    dividends_accrued = 0.0
    total_dividends = 0.0
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        _div_cache = get_cached_portfolio_account()
        dividends_accrued = _div_cache.get("accrued_dividends", 0.0)
        total_dividends = _div_cache.get("dividends_ytd", 0.0)
    except Exception:
        pass

    # Dividend yield YTD — dividend-tier holdings only
    dividends_yield_pct = 0.0
    try:
        from src.portfolio.models import PortfolioHolding as _PH
        with get_db() as db:
            div_holdings = db.query(_PH).filter(
                _PH.tier == "dividend",
                _PH.shares > 0
            ).all()
        div_cost_basis = sum(h.total_invested for h in div_holdings)
        if div_cost_basis > 0:
            dividends_yield_pct = ((total_dividends + dividends_accrued) / div_cost_basis) * 100
    except Exception:
        pass

    market_status = _get_state("market_status") or "unknown"
    market_pct = _get_state("market_pct_above_sma") or ""

    # Performance data
    perf = _build_portfolio_performance()
    tiers = _build_tier_breakdown(holdings, fx_rates)
    performers = _build_top_performers(holdings)

    # Get portfolio account data from cache (populated hourly by scheduler)
    portfolio_margin_pct = 0.0
    portfolio_maintenance_margin = 0.0
    portfolio_nlv = 0.0
    portfolio_buying_power = 0.0
    portfolio_loans = 0.0
    portfolio_accrued_interest = 0.0
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        cached = get_cached_portfolio_account()
        portfolio_nlv = cached.get("nlv", 0.0)
        portfolio_maintenance_margin = cached.get("margin", 0.0)
        portfolio_buying_power = cached.get("buying_power", 0.0)
        portfolio_margin_pct = cached.get("margin_pct", 0.0)
        # LOANS card = actual IBKR margin loan (amount borrowed), not the signed cash balance.
        # cached["loans"] is TotalCashBalance (signed: +cash / -debit); borrowed = max(0, -that),
        # so a cash-positive account shows 0 instead of mislabeling its cash as a loan.
        portfolio_loans = max(0.0, -cached.get("loans", 0.0))
        portfolio_accrued_interest = cached.get("accrued_interest", 0.0)
    except Exception:
        pass

    # Compute adaptive cap values for risk panel display
    _nlv = portfolio_nlv or 0.0
    try:
        from src.core.config import get_settings
        _pcfg = get_settings().portfolio
        position_cap = round(min(_nlv * _pcfg.position_cap_pct, _pcfg.position_cap_max_usd), 0)
        total_exposure_cap = round(min(_nlv * _pcfg.total_exposure_pct, _pcfg.total_exposure_max_usd), 0)
        daily_deployment_cap = round(min(_nlv * _pcfg.daily_deployment_pct, _pcfg.daily_deployment_max_usd), 0)
    except Exception:
        position_cap = 0.0
        total_exposure_cap = 0.0
        daily_deployment_cap = 0.0

    # Open orders from portfolio IBKR (cached, non-blocking)
    portfolio_open_orders = []
    try:
        from src.portfolio.connection import get_cached_portfolio_open_orders
        portfolio_open_orders = get_cached_portfolio_open_orders()
    except Exception:
        pass

    portfolio_pending_orders = []
    try:
        from src.portfolio.connection import get_cached_portfolio_pending_orders
        portfolio_pending_orders = get_cached_portfolio_pending_orders()
    except Exception:
        pass

    # Compounder signal table — computed live from the (freshly-priced) watchlist rows so the
    # full ranked universe always shows, independent of whether a trading scan ran.
    _compounder_signals = []
    if _get_state("strategy") == "compounder":
        try:
            from src.portfolio import compounder as _cmp
            from src.core.config import get_settings as _gs
            _pcfg = _gs().portfolio
            _cc = _pcfg.compounder
            _tier_alloc = {
                "breakthrough": _cc.tier_breakthrough,
                "dividend": _cc.tier_dividend,
                "growth": _cc.tier_growth,
            }
            # FX-normalise holdings to the account BASE currency before comparing to base-ccy targets —
            # a £-priced LSE holding measured against a €-target without conversion mis-sizes it by the
            # FX rate (matches the live engine's _get_holdings_map). nlv is already base.
            _held = {h.symbol: _to_base(h.market_value or h.total_invested or 0, h.currency,
                                        fx_rates, _base_ccy) for h in holdings}
            _compounder_signals = _cmp.build_signals_from_watchlist(
                watchlist, _held, portfolio_nlv or 0, _cc, _tier_alloc)
        except Exception as _e:
            log.warning("compounder_dashboard_signals_failed", error=str(_e))

    # Pending portfolio suggestions — chip moved here from the options dashboard header
    pending_portfolio = 0
    try:
        from src.core.suggestions import TradeSuggestion
        with get_db() as _db:
            pending_portfolio = _db.query(TradeSuggestion).filter(
                TradeSuggestion.status.in_(["pending", "submitted", "approved", "queued"]),
                TradeSuggestion.source == "portfolio",
            ).count()
    except Exception as _e:
        log.warning("portfolio_pending_count_failed", error=str(_e))

    # Live daily budget: the scan's per-DAY pace minus capital already committed TODAY (filled buys +
    # still-working BUY orders). Computed every page load so the budget card moves with the buys, instead
    # of only refreshing at the 2-hourly scan (which left it showing the full budget right after several
    # fills). CRITICAL: this MUST use the same pace math as the scan (compounder.base_daily_pace) — the
    # lump-defusal horizon stretch AND the froth throttle — or the card overstates the budget. A naive
    # investable*base_pct/dca_horizon ignored both and showed ~$15k when the real deployable pace was
    # ~$6k (lump-stretched to ~45d) throttled to ~$1.4k after today's fills — "shows 10k, buys nothing".
    _live_daily_budget = float(_get_state("compounder_daily_budget") or 0)
    try:
        from datetime import datetime as _dt_t
        from sqlalchemy import func as _func2
        from src.portfolio.config import CompounderConfig as _CC
        from src.portfolio.compounder import base_daily_pace as _base_daily_pace
        from src.portfolio.connection import get_cached_portfolio_pending_orders as _gp
        _investable = float(_get_state("compounder_investable") or 0)
        if _investable > 0:
            _ccfg = _CC()
            _throttle = float(_get_state("compounder_pace_throttle") or 1.0)
            # UTC boundary — match the scan's throttle (buyer.py uses datetime.utcnow) and the UTC
            # created_at storage, so the live budget card and the actual per-day cap agree on "today".
            _start = _dt_t.utcnow().strftime("%Y-%m-%d") + " 00:00:00"
            with get_db() as _bdb:
                # amount is in each fill's LOCAL currency — group by currency and FX-normalise to base
                # (a raw SUM mixes £/$/€); base must match the base-ccy pace below (see src.portfolio.fx).
                _fills_rows = _bdb.query(
                    PortfolioTransaction.currency,
                    _func2.coalesce(_func2.sum(PortfolioTransaction.amount), 0.0),
                ).filter(PortfolioTransaction.action == "buy",
                         PortfolioTransaction.created_at >= _start,
                         # Parking cash into the yield ETF isn't deployment — exclude it (mirrors the
                         # engine's _fills_today) so a ~€400k park doesn't zero the displayed budget.
                         PortfolioTransaction.symbol != _park_symbol,
                         ).group_by(PortfolioTransaction.currency).all()
                _fills_today = sum(_to_base(_amt, _ccy, fx_rates, _base_ccy)
                                   for _ccy, _amt in _fills_rows)
            _open_buy_notional = 0.0
            for _o in _gp() or []:
                if _o.get("sec_type") == "STK" and _o.get("action") == "BUY":
                    _px = float(_o.get("limit_price") or 0)
                    _occy = str(_o.get("currency") or "").upper()
                    # GBP/LSE limit prices are in PENCE — to pounds first, then FX-normalise local→base.
                    if _occy == "GBP":
                        _px = _px / 100.0
                    _open_buy_notional += _to_base(float(_o.get("remaining") or 0) * _px,
                                                   _occy, fx_rates, _base_ccy)
            # Replicate the scan's daily_deploy_budget non-crash branch: start-of-day gap drives the
            # lump-stretched horizon, throttle slows the pace, then subtract what's gone out today.
            _live_target = float(_get_state("compounder_live_target") or 0)
            _deployed = float(_get_state("compounder_deployed") or 0)
            _remaining_gap = max(0.0, _live_target - _deployed - _open_buy_notional)
            _deployed_today = _fills_today + _open_buy_notional
            _base_pace = _base_daily_pace(_investable, _ccfg.base_pct, _ccfg.dca_horizon_days,
                                          _remaining_gap, _deployed_today,
                                          _ccfg.lump_horizon_days, _throttle)
            _live_daily_budget = max(0.0, min(_remaining_gap, _base_pace - _deployed_today))
    except Exception as _e:
        log.warning("compounder_live_budget_failed", error=str(_e))

    # Stale-invested guard: a silent Flex failure must never again let an unbooked deposit read as
    # return. Flag when the deposit sync hasn't succeeded in >36h (nightly success keeps it <24h) AND
    # there's a >2% NLV-over-invested gap that an unbooked deposit could explain. Self-clears once Flex
    # syncs or the gap closes; the wording tells the operator the invested base may be missing deposits.
    flex_stale = False
    flex_stale_msg = ""
    try:
        from datetime import datetime as _dts
        _last = _get_state(f"flex_last_success_{_pacct}") if _pacct else ""
        _age_h = None
        if _last:
            try:
                _age_h = (_dts.utcnow() - _dts.fromisoformat(_last)).total_seconds() / 3600.0
            except Exception:
                _age_h = None
        _gap = (portfolio_nlv or 0) - (total_invested or 0)
        if _gap > max(2000.0, 0.02 * (total_invested or 0)) and (_age_h is None or _age_h > 36):
            flex_stale = True
            _ago = "never" if _age_h is None else f"{_age_h:.0f}h ago"
            flex_stale_msg = (
                f"Deposit sync (Flex) last succeeded {_ago}. The invested base may be missing a recent "
                f"deposit (~{_gap:,.0f} {_base_ccy} gap), which would read as return until it syncs.")
    except Exception as _e:
        log.warning("flex_stale_check_failed", error=str(_e))

    # Tier target %s for the pie caption — read live from the ACTIVE strategy's config so the caption
    # never drifts from the engine. Compounder trimmed dividend (5/65/30) vs the classic TierAllocation
    # (15/60/25); pick whichever is driving the book.
    try:
        from src.core.config import get_settings as _gs2
        _pc = _gs2().portfolio
        if _get_state("strategy") == "compounder":
            _cc2 = _pc.compounder
            tier_targets = {"dividend": _cc2.tier_dividend, "growth": _cc2.tier_growth,
                            "breakthrough": _cc2.tier_breakthrough}
        else:
            _ta = _pc.tier_allocation
            tier_targets = {"dividend": _ta.dividend, "growth": _ta.growth,
                            "breakthrough": _ta.breakthrough}
    except Exception as _e:
        log.warning("tier_targets_failed", error=str(_e))
        tier_targets = {"dividend": 0.05, "growth": 0.65, "breakthrough": 0.30}

    return templates.TemplateResponse("portfolio.html", {
        "tier_targets": tier_targets,
        "flex_stale": flex_stale,
        "flex_stale_msg": flex_stale_msg,
        "request": request,
        "pending_portfolio": pending_portfolio,
        "holdings": holdings,
        "watchlist": watchlist,
        "transactions": transactions,
        "put_entries": put_entries,
        "total_invested": total_invested,
        "total_value": total_value,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "total_dividends": total_dividends,
        "dividends_accrued": dividends_accrued,
        "dividends_yield_pct": dividends_yield_pct,
        "num_holdings": len(holdings),
        "portfolio_margin_pct": portfolio_margin_pct,
        "portfolio_maintenance_margin": portfolio_maintenance_margin,
        "portfolio_nlv": portfolio_nlv,
        "portfolio_buying_power": portfolio_buying_power,
        "portfolio_loans": portfolio_loans,
        "portfolio_accrued_interest": portfolio_accrued_interest,
        "market_status": market_status,
        "market_pct": market_pct,
        # Performance chart
        "perf_labels": perf["labels"],
        "perf_portfolio": perf["portfolio_data"],
        "perf_brkb": perf["brkb_data"],
        "current_return_pct": perf.get("current_return_pct", 0.0),
        "total_invested_usd": perf.get("total_invested_usd", 0.0),
        # Tier breakdown
        "tier_dividend": tiers.get("dividend", 0),
        "tier_growth": tiers.get("growth", 0),
        "tier_breakthrough": tiers.get("breakthrough", 0),
        "tier_cash": tiers.get("cash", 0),
        # Top performers (gainers) vs underperformers (losers), split by SIGN — not a fixed
        # top-5/bottom-5 slice (which hid the losers card entirely with <=5 holdings and could show
        # a name that's actually down as a "top performer"). `performers` is sorted P&L%-desc.
        "top_performers": [p for p in performers if p["pnl_pct"] >= 0][:5],
        "bottom_performers": sorted([p for p in performers if p["pnl_pct"] < 0],
                                    key=lambda x: x["pnl_pct"])[:5],
        "portfolio_open_orders": portfolio_open_orders,
        "portfolio_pending_orders": portfolio_pending_orders,
        "position_cap": position_cap,
        "total_exposure_cap": total_exposure_cap,
        "daily_deployment_cap": daily_deployment_cap,
        # Compounder strategy reserve state (cards shown only when active)
        "is_compounder": (_get_state("strategy") == "compounder"),
        "compounder": {
            # Live deployed = current market value of holdings (base ccy). Was read from the persisted
            # compounder_deployed state, which only the periodic scan refreshes — so it lagged behind
            # fills/price updates. total_value is recomputed every page load from the holdings table.
            "deployed": total_value - parked_amount,   # parked cash-yield ETF is a reserve, not deployed stock
            "live_target": float(_get_state("compounder_live_target") or 0),
            "investable": float(_get_state("compounder_investable") or 0),
            "daily_budget": _live_daily_budget,
            "drawdown_pct": float(_get_state("compounder_drawdown_pct") or 0),
            "tranches_fired": int(float(_get_state("compounder_tranches_fired") or 0)),
            "unlocked_pct": float(_get_state("compounder_reserve_unlocked_pct") or 0),
            "reserve_peak": float(_get_state("compounder_reserve_peak") or 0),
        },
        "compounder_signals_list": _compounder_signals,
        "wl_map": {w.symbol: w for w in watchlist},
        "compounder_active_signals": sum(1 for s in _compounder_signals if s.get("action") in ("direct", "put")),
        # Slots = target names already held / total target names (accumulation progress)
        "compounder_slots_allowed": sum(1 for s in _compounder_signals if (s.get("target") or 0) > 0),
        "compounder_slots_filled": sum(1 for s in _compounder_signals if (s.get("target") or 0) > 0 and (s.get("current") or 0) > 0),
        # Parked cash (money-market ETF reserve) — top-of-page card
        "parked_symbol": _park_symbol,
        "parked_amount": parked_amount,
        "parked_annual_pct": _park_annual_pct,
        "parked_base_sym": parked_base_sym,
    })


@router.get("/portfolio/trades", response_class=HTMLResponse)
async def portfolio_trades_page(request: Request):
    """Full trade history for long-term portfolio."""
    with get_db() as db:
        # All transactions, most recent first
        transactions = db.query(PortfolioTransaction).order_by(
            PortfolioTransaction.created_at.desc()
        ).all()

        # Summary stats
        buys = [t for t in transactions if t.action == "buy"]
        sells = [t for t in transactions if t.action == "sell"]
        dividends = [t for t in transactions if t.action == "dividend"]
        put_entries = [t for t in transactions if t.action in ("sell_put", "buy_put", "put_assigned", "put_expired")]
        call_entries = [t for t in transactions if t.action in ("sell_call", "buy_call", "call_expired", "call_assigned")]
        option_entries = put_entries + call_entries

        total_bought = sum(t.amount for t in buys)
        total_sold = sum(t.amount for t in sells)
        total_dividends = sum(t.amount for t in dividends)
        total_premium = sum(t.premium_collected or 0 for t in option_entries)

        # Per-symbol summary
        from collections import defaultdict
        symbol_stats: dict[str, dict] = defaultdict(lambda: {
            "buys": 0, "sells": 0, "dividends": 0, "shares_bought": 0,
            "shares_sold": 0, "avg_buy": 0.0, "total_invested": 0.0,
        })
        for t in transactions:
            s = symbol_stats[t.symbol]
            if t.action == "buy":
                s["buys"] += 1
                s["shares_bought"] += t.shares
                s["total_invested"] += t.amount
            elif t.action == "sell":
                s["sells"] += 1
                s["shares_sold"] += t.shares
            elif t.action == "dividend":
                s["dividends"] += t.amount

        for sym, s in symbol_stats.items():
            if s["shares_bought"] > 0:
                s["avg_buy"] = s["total_invested"] / s["shares_bought"]

        # Sort by total invested descending
        symbol_summary = sorted(
            symbol_stats.items(),
            key=lambda x: x[1]["total_invested"],
            reverse=True,
        )

    return templates.TemplateResponse("portfolio_trades.html", {
        "request": request,
        "transactions": transactions,
        "total_count": len(transactions),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "dividend_count": len(dividends),
        "put_entry_count": len(put_entries),
        "total_bought": total_bought,
        "total_sold": total_sold,
        "total_dividends": total_dividends,
        "total_premium": total_premium,
        "symbol_summary": symbol_summary,
    })


@router.post("/portfolio/trades/sync")
async def sync_portfolio_trades():
    """Manually trigger IBKR trade sync for portfolio — fires in background, returns immediately."""
    import threading
    from fastapi.responses import RedirectResponse
    from src.core.config import get_settings

    def _run_sync():
        try:
            from src.portfolio.scheduler import job_portfolio_sync_trades
            pcfg = get_settings().portfolio
            job_portfolio_sync_trades(pcfg)
        except Exception:
            import traceback
            traceback.print_exc()

    threading.Thread(target=_run_sync, daemon=True).start()
    return RedirectResponse(url="/portfolio/trades", status_code=303)


@router.post("/api/portfolio/sync-injections")
async def sync_capital_injections():
    """Sync deposit history from IBKR Flex Web Service."""
    try:
        from src.portfolio.capital_injections import sync_injections_from_ibkr
        added = sync_injections_from_ibkr(include_withdrawals=True)
        return {"status": "ok", "added": added,
                "message": f"Synced {added} new deposit/withdrawal row(s) from IBKR"}
    except ValueError as e:
        return {"status": "setup_needed", "message": str(e)}
    except Exception as e:
        log.error("sync_injections_error", error=str(e))
        return {"status": "error", "message": str(e)}
