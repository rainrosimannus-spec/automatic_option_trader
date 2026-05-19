"""
Financial Modeling Prep (FMP) API client.
Provides fundamental data: revenue growth, dividends, payout ratio, debt metrics.
Free tier: 250 requests/day — cache aggressively.
Cache: data/fmp_cache.json — 30 days per symbol, survives restarts and re-runs.
"""
from __future__ import annotations

import json
import time
import threading
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from src.core.logger import get_logger

log = get_logger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"
_CACHE_FILE = Path("data/fmp_cache.json")
_CACHE_TTL_DAYS = 30


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.warning("fmp_cache_save_failed", error=str(e))


def _cache_get(symbol: str) -> Optional[dict]:
    cache = _load_cache()
    entry = cache.get(symbol)
    if not entry:
        return None
    age_days = (time.time() - entry.get("cached_at", 0)) / 86400
    if age_days > _CACHE_TTL_DAYS:
        return None
    return entry.get("data")


def _cache_set(symbol: str, data: dict) -> None:
    cache = _load_cache()
    cache[symbol] = {"cached_at": time.time(), "data": data}
    _save_cache(cache)


def get_fmp_key() -> Optional[str]:
    """Get FMP API key from settings.yaml."""
    try:
        from src.core.config import get_settings
        key = get_settings().raw.get("fmp", {}).get("api_key")
        if key:
            return key
    except Exception:
        pass
    import os
    return os.environ.get("FMP_API_KEY")


def _get(endpoint: str, symbol: str, params: dict = {}) -> Optional[dict | list]:
    """Make a GET request to FMP API."""
    key = get_fmp_key()
    if not key:
        log.error("fmp_api_key_missing")
        return None
    try:
        all_params = {"symbol": symbol, "apikey": key}
        all_params.update(params)
        resp = requests.get(f"{FMP_BASE}/{endpoint}", params=all_params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("fmp_request_failed", endpoint=endpoint, error=str(e))
        return None


def get_income_growth(symbol: str, years: int = 3) -> Optional[dict]:
    data = _get("income-statement", symbol, {"limit": years + 1, "period": "annual"})
    if not data or len(data) < 2:
        return None
    try:
        latest = data[0]["revenue"]
        prior = data[1]["revenue"]
        oldest = data[-1]["revenue"]
        yoy = (latest - prior) / abs(prior) * 100 if prior else 0
        avg = (latest - oldest) / abs(oldest) * 100 / years if oldest and years > 0 else 0
        return {
            "revenue_latest": latest,
            "revenue_yoy_pct": round(yoy, 1),
            "revenue_avg_annual_pct": round(avg, 1),
        }
    except Exception as e:
        log.warning("fmp_income_parse_failed", symbol=symbol, error=str(e))
        return None


def get_dividend_data(symbol: str) -> Optional[dict]:
    data = _get("ratios", symbol, {"limit": 3, "period": "annual"})
    if not data or len(data) < 1:
        return None
    try:
        latest = data[0]
        result = {
            "dividend_yield": round((latest.get("dividendYield") or 0) * 100, 2),
            "payout_ratio": round((latest.get("payoutRatio") or 0) * 100, 1),
            "dividend_cut": False,
        }
        if len(data) >= 2:
            prev_yield = (data[1].get("dividendYield") or 0) * 100
            curr_yield = result["dividend_yield"]
            if prev_yield > 0.5 and curr_yield < prev_yield * 0.7:
                result["dividend_cut"] = True
        return result
    except Exception as e:
        log.warning("fmp_dividend_parse_failed", symbol=symbol, error=str(e))
        return None


def get_debt_metrics(symbol: str) -> Optional[dict]:
    data = _get("balance-sheet-statement", symbol, {"limit": 3, "period": "annual"})
    cf_data = _get("cash-flow-statement", symbol, {"limit": 3, "period": "annual"})
    if not data:
        return None
    try:
        latest = data[0]
        total_debt = latest.get("totalDebt") or 0
        equity = latest.get("totalStockholdersEquity") or 1
        de_ratio = total_debt / abs(equity) if equity else 0
        fcf_negative_years = 0
        if cf_data:
            for cf in cf_data:
                fcf = (cf.get("freeCashFlow") or 0)
                if fcf < 0:
                    fcf_negative_years += 1
        return {
            "debt_to_equity": round(de_ratio, 2),
            "fcf_negative_years": fcf_negative_years,
        }
    except Exception as e:
        log.warning("fmp_debt_parse_failed", symbol=symbol, error=str(e))
        return None


def get_year_high(symbol: str) -> Optional[float]:
    data = _get("quote", symbol)
    if not data:
        return None
    try:
        item = data[0] if isinstance(data, list) else data
        year_high = item.get("yearHigh")
        if year_high and float(year_high) > 0:
            return float(year_high)
    except Exception as e:
        log.warning("fmp_year_high_parse_failed", symbol=symbol, error=str(e))
    return None


def get_full_fundamentals(symbol: str) -> Optional[dict]:
    """
    Fetch all fundamental metrics needed for screening.
    Results cached per symbol for 30 days in data/fmp_cache.json.
    Cache survives restarts and re-runs — zero quota burn on repeat calls.
    """
    # Check cache first
    cached = _cache_get(symbol)
    if cached is not None:
        log.info("fmp_cache_hit", symbol=symbol)
        return cached

    income = get_income_growth(symbol)
    dividends = get_dividend_data(symbol)
    debt = get_debt_metrics(symbol)

    if not any([income, dividends, debt]):
        return None

    result = {}
    if income:
        result.update(income)
    if dividends:
        result.update(dividends)
    if debt:
        result.update(debt)

    # Cache the result
    _cache_set(symbol, result)
    log.info("fmp_fundamentals_fetched", symbol=symbol, metrics=list(result.keys()))
    return result


def clear_cache(symbol: str = None) -> None:
    """Clear cache for one symbol or entirely. Call before forced re-fetch."""
    if symbol is None:
        _CACHE_FILE.unlink(missing_ok=True)
        log.info("fmp_cache_cleared_all")
    else:
        cache = _load_cache()
        cache.pop(symbol, None)
        _save_cache(cache)
        log.info("fmp_cache_cleared_symbol", symbol=symbol)


# ── Earnings calendar (bulk refresh + per-symbol lookup) ──────────────
# Ported from son's 1cf5ef0 (MildConcussion/mesicap_trader) and adapted
# to the father's existing EarningsCache schema (`status` field; no `source`
# column). FMP is the sole earnings data source post-port — the IBKR
# CalendarReport path is dead on these gateways (Error 10276 'News feed
# is not allowed' verified 2026-05-19).

_EARNINGS_REFRESH_TTL_HOURS = 24
_EARNINGS_REFRESH_KEY = "earnings_calendar_last_refresh"
_EARNINGS_REFRESH_LOCK = threading.Lock()
_FMP_PAGE_LIMIT = 4000  # per FMP docs


def _earnings_get_last_refresh() -> Optional[datetime]:
    try:
        from src.core.database import get_db
        from src.core.models import SystemState
        with get_db() as session:
            row = session.get(SystemState, _EARNINGS_REFRESH_KEY)
            if row is None or not row.value:
                return None
            return datetime.fromisoformat(row.value)
    except Exception as e:
        log.debug("earnings_refresh_state_read_failed", error=str(e))
        return None


def _earnings_set_last_refresh(dt: datetime) -> None:
    try:
        from src.core.database import get_db
        from src.core.models import SystemState
        with get_db() as session:
            row = session.get(SystemState, _EARNINGS_REFRESH_KEY)
            if row is None:
                row = SystemState(key=_EARNINGS_REFRESH_KEY, value=dt.isoformat())
                session.add(row)
            else:
                row.value = dt.isoformat()
    except Exception as e:
        log.warning("earnings_refresh_state_write_failed", error=str(e))


def _fetch_earnings_page(from_date: date, to_date: date, page: int) -> Optional[list]:
    """Single FMP earnings-calendar page request. Returns [] on success-empty,
    None on failure (so caller can distinguish)."""
    key = get_fmp_key()
    if not key:
        log.error("fmp_api_key_missing")
        return None
    try:
        params = {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "page": page,
            "apikey": key,
        }
        resp = requests.get(f"{FMP_BASE}/earnings-calendar", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("fmp_earnings_calendar_request_failed",
                     page=page, error=str(e))
        return None


def refresh_earnings_calendar(days_ahead: int = 7, force: bool = False) -> bool:
    """Bulk-fetch FMP earnings-calendar for the next `days_ahead` days and
    repopulate the earnings_cache table. Returns True on success.

    Default 7 days = gate's `earnings_avoid_days` (3) + 24h refresh staleness +
    ~1d buffer. Pagination implemented but 7-day windows fit in a single page
    in practice. Writes rows compatible with the father's existing schema
    (status='found'; no `source` column). FMP is the sole source post-port,
    so the table is wiped before repopulating.
    """
    with _EARNINGS_REFRESH_LOCK:
        if not force:
            last = _earnings_get_last_refresh()
            if last and datetime.utcnow() - last < timedelta(hours=_EARNINGS_REFRESH_TTL_HOURS):
                log.debug("earnings_refresh_skipped_recent",
                           last=last.isoformat())
                return True

        today = date.today()
        to_date = today + timedelta(days=days_ahead)

        all_records: list[dict] = []
        for page in range(20):  # hard cap to prevent runaway
            chunk = _fetch_earnings_page(today, to_date, page)
            if chunk is None:
                # Network/auth/HTTP failure — abort without stamping refresh.
                log.warning("earnings_refresh_aborted", page=page)
                return False
            if not chunk:
                break
            all_records.extend(chunk)
            if len(chunk) < _FMP_PAGE_LIMIT:
                break
        else:
            log.warning("earnings_refresh_pagination_overflow",
                         pages_consumed=20)

        # Pick earliest future date per symbol
        next_by_symbol: dict[str, date] = {}
        for rec in all_records:
            sym = rec.get("symbol")
            d_str = rec.get("date")
            if not sym or not d_str:
                continue
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < today:
                continue
            existing = next_by_symbol.get(sym)
            if existing is None or d < existing:
                next_by_symbol[sym] = d

        # Repopulate cache atomically. FMP is sole source post-port — wipe all.
        try:
            from src.core.database import get_db
            from src.core.models import EarningsCache
            now = datetime.utcnow()
            with get_db() as session:
                session.query(EarningsCache).delete()
                for sym, d in next_by_symbol.items():
                    session.add(EarningsCache(
                        symbol=sym,
                        next_earnings_date=d.isoformat(),
                        status="found",  # father's schema
                        fetched_at=now,
                    ))
        except Exception as e:
            log.error("earnings_refresh_db_write_failed", error=str(e))
            return False

        _earnings_set_last_refresh(datetime.utcnow())
        log.info("earnings_refresh_succeeded",
                  records=len(all_records),
                  symbols=len(next_by_symbol),
                  days_ahead=days_ahead)
        return True


def get_next_earnings_date(symbol: str) -> tuple[Optional[date], bool]:
    """Return (next_earnings_date_or_None, refresh_ok).

    `refresh_ok` is True iff the cache reflects a successful refresh within
    the last 24h. If False, callers must treat absence-of-row as 'we don't
    know' rather than 'no upcoming earnings' (fail-CLOSED).

    A row missing for a symbol with `refresh_ok=True` is positive evidence
    the symbol has no earnings inside the refresh window (default 7 days).
    """
    refresh_ok = refresh_earnings_calendar()

    try:
        from src.core.database import get_db
        from src.core.models import EarningsCache
        with get_db() as session:
            row = session.get(EarningsCache, symbol)
            if row is None or not row.next_earnings_date:
                return (None, refresh_ok)
            try:
                return (datetime.strptime(row.next_earnings_date, "%Y-%m-%d").date(),
                        refresh_ok)
            except ValueError:
                return (None, refresh_ok)
    except Exception as e:
        log.warning("earnings_cache_read_failed", symbol=symbol, error=str(e))
        return (None, False)
