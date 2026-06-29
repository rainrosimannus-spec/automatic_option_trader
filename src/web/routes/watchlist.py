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

    signals, wl_map = [], {}
    try:
        with get_db() as db:
            rows = db.query(PortfolioWatchlist).all()
            holds = db.query(PortfolioHolding).filter(PortfolioHolding.shares > 0).all()
        wl_map = {w.symbol: w for w in rows}
        # FX-normalise holdings to the account BASE currency (investable/nlv are base) so a foreign
        # holding isn't measured against its base-ccy target unconverted (see src.portfolio.fx).
        from src.portfolio import fx as _pfx
        _rates = _pfx.load_fx_rates()
        held = {h.symbol: _pfx.to_base(h.market_value or h.total_invested or 0, h.currency, _rates)
                for h in holds}
        inv = _num("compounder_investable")
        nlv = inv / (1 - cc.cash_buffer_pct) if inv > 0 else (sum(held.values()) or 1.0)
        signals = cmp.build_signals_from_watchlist(rows, held, nlv, cc, tier_alloc)
    except Exception as e:
        log.warning("watchlist_signals_failed", error=str(e))

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

    slots_allowed = sum(1 for s in signals if (s.get("target") or 0) > 0)
    slots_filled = sum(1 for s in signals if (s.get("target") or 0) > 0 and (s.get("current") or 0) > 0)

    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "signals": signals,
        "wl_map": wl_map,
        "tier_summary": tier_summary,
        "reserve": reserve,
        "strategy": strategy,
        "is_compounder": strategy == "compounder",
        "slots_filled": slots_filled,
        "slots_allowed": slots_allowed,
    })
