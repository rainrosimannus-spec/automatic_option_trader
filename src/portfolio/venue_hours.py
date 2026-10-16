"""Venue sessions: WHEN each market the compounder buys in is actually open.

Two layers, one answer:

1. A static table, per settlement currency, in venue-local wall-clock time and to the MINUTE,
   copied from IBKR's own `ContractDetails.liquidHours` on 2026-10-16 (one contract per watchlist
   venue). Lunch breaks are real segments (Tokyo, Hong Kong, Singapore); the close INCLUDES the
   closing auction, because that is where IBKR says a limit can still fill (ASX 16:11, SEHK 16:10,
   TASE 17:25, AEB 17:40, LSE 16:50 …). Before this the table was hour-granular and rounded every
   close DOWN — Tokyo "15" for 15:30, Sydney "16" for 16:11, Amsterdam "17" for 17:40 — and Oslo was
   rounded UP (17 for 16:30), which would have placed orders into a shut venue.

2. A live overlay filled from the same `liquidHours` field whenever we have contract details in hand
   (order placement, review sales) and once per scan for every venue on the watchlist. IBKR lists
   each day for the coming week or so, with holidays and half-days marked ("20261019:CLOSED" — Hong
   Kong's Chung Yeung Day 2026-10-19 — or "20261016:0959-20261016:1350", TASE's Friday). The static
   table cannot know those; the overlay does, and it wins for any date it covers.

Everything here is pure data + parsing. The clock-reading gates (`_market_open`, `_late_session`,
`_aftermarket`) live in buyer.py so that tests can freeze ONE clock (`buyer.datetime`).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable

import pytz

from src.core.logger import get_logger

log = get_logger(__name__)

MON_FRI = (0, 1, 2, 3, 4)

# currency -> (pytz zone, sessions ((open, close), ...), trading weekdays). Venue-local wall clock.
# Source: IBKR liquidHours 2026-10-16 (ESLT@TASE, 4062@TSEJ, 2269@SEHK, S68@SGX, PME@ASX, NKT@CPH,
# ASML@AEB, SAP@IBIS, VER@VSE, MC@SBF, ENEL@BVME, SAN@BM, ABF@LSE, NESN@EBS, EQNR@OSE, NPN@JSE,
# RELIANCE@NSE, 035420@KRX, WALMEX@MEXI, GOOG/ABNB@SMART, BMO@SMART). SEK: no SFB symbol resolved on
# this login — Nasdaq Stockholm's published 09:00–17:30 (closing auction ends 17:30).
MARKET_HOURS: dict[str, tuple[str, tuple[tuple[str, str], ...], tuple[int, ...]]] = {
    "USD": ("US/Eastern",          (("09:30", "16:00"),), MON_FRI),
    "CAD": ("US/Eastern",          (("09:30", "16:00"),), MON_FRI),
    "MXN": ("US/Central",          (("08:30", "15:00"),), MON_FRI),   # BMV keeps step with NYSE; IBKR zone US/Central
    "EUR": ("Europe/Berlin",       (("09:00", "17:40"),), MON_FRI),   # earliest EUR close: AEB/SBF 17:40 (BVME 17:42, IBIS/VSE/BM 17:45)
    "CHF": ("Europe/Zurich",       (("09:00", "17:40"),), MON_FRI),
    "GBP": ("Europe/London",       (("08:00", "16:50"),), MON_FRI),
    "NOK": ("Europe/Oslo",         (("09:00", "16:30"),), MON_FRI),
    "SEK": ("Europe/Stockholm",    (("09:00", "17:30"),), MON_FRI),
    "DKK": ("Europe/Copenhagen",   (("09:00", "17:00"),), MON_FRI),
    "ZAR": ("Africa/Johannesburg", (("08:30", "17:15"),), MON_FRI),
    "ILS": ("Asia/Jerusalem",      (("09:59", "17:25"),), MON_FRI),   # Mon–Fri since 2026; Friday closes 13:50 (EARLY_CLOSE)
    "INR": ("Asia/Kolkata",        (("09:15", "15:30"),), MON_FRI),   # IBKR: 09:15–15:15 + 15:20–15:30 closing session
    "KRW": ("Asia/Seoul",          (("09:00", "15:30"),), MON_FRI),
    "JPY": ("Asia/Tokyo",          (("09:00", "11:30"), ("12:30", "15:30")), MON_FRI),
    "HKD": ("Asia/Hong_Kong",      (("09:30", "12:00"), ("13:00", "16:10")), MON_FRI),
    "SGD": ("Asia/Singapore",      (("09:00", "12:00"), ("13:00", "17:16")), MON_FRI),
    "AUD": ("Australia/Sydney",    (("09:59", "16:11"),), MON_FRI),
}

# currency -> {weekday: close "HH:MM"} — a standing weekly early close (the overlay handles one-offs).
EARLY_CLOSE: dict[str, dict[int, str]] = {
    "ILS": {4: "13:50"},
}


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _localize(tz: pytz.BaseTzInfo, d: date, t: time) -> datetime:
    return tz.localize(datetime.combine(d, t))


def static_sessions(currency: str | None, d: date) -> tuple[tuple[datetime, datetime], ...] | None:
    """Aware (start, end) pairs for `currency`'s venue on local date `d` from the static table —
    () on a non-trading weekday, None for an unmapped currency."""
    spec = MARKET_HOURS.get((currency or "").upper())
    if not spec:
        return None
    tz_name, segments, days = spec
    if d.weekday() not in days:
        return ()
    tz = pytz.timezone(tz_name)
    early = EARLY_CLOSE.get((currency or "").upper(), {}).get(d.weekday())
    out: list[tuple[datetime, datetime]] = []
    for i, (o, c) in enumerate(segments):
        if early and i == len(segments) - 1:
            c = early
        start, end = _localize(tz, d, hhmm(o)), _localize(tz, d, hhmm(c))
        if early and end <= start:          # early close before the last segment opens → drop it
            continue
        out.append((start, end))
    return tuple(out)


# ── live overlay from IBKR liquidHours ────────────────────────────────────────────────────

# (currency, exchange) -> {"YYYYMMDD": ((start, end), ...)}; exchange None = merged per-currency view.
_CALENDAR: dict[tuple[str, str | None], dict[str, tuple[tuple[datetime, datetime], ...]]] = {}
_CALENDAR_TZ: dict[tuple[str, str | None], str] = {}
_LAST_REFRESH: datetime | None = None


def parse_liquid_hours(text: str, tz_name: str) -> dict[str, tuple[tuple[datetime, datetime], ...]]:
    """IBKR's 'YYYYMMDD:HHMM-YYYYMMDD:HHMM;YYYYMMDD:CLOSED;…' → {date: aware sessions}. Several segments
    on one day (lunch break) are listed as separate entries and collected in order. Unparseable
    pieces are skipped, never fatal."""
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        return {}
    for piece in (text or "").split(";"):
        piece = piece.strip()
        if not piece:
            continue
        try:
            if piece.endswith("CLOSED"):
                out.setdefault(piece[:8], [])
                continue
            a, b = piece.split("-")
            d1, t1 = a.split(":")
            d2, t2 = b.split(":")
            start = tz.localize(datetime.strptime(d1 + t1, "%Y%m%d%H%M"))
            end = tz.localize(datetime.strptime(d2 + t2, "%Y%m%d%H%M"))
            if end > start:
                out.setdefault(d1, []).append((start, end))
        except Exception:
            continue
    return {d: tuple(sorted(s)) for d, s in out.items()}


def _merge_currency(currency: str) -> None:
    """Per-currency view over every exchange noted for it: a date is CLOSED only when every venue
    says so; otherwise the open venue with the EARLIEST final close sets the day (conservative —
    an order placed by then can still fill on every open venue of that currency)."""
    members = [(k, v) for k, v in _CALENDAR.items() if k[0] == currency and k[1] is not None]
    if not members:
        return
    dates: set[str] = set()
    for _k, v in members:
        dates |= set(v)
    merged: dict[str, tuple[tuple[datetime, datetime], ...]] = {}
    for d in dates:
        opens = [v[d] for _k, v in members if d in v and v[d]]
        merged[d] = min(opens, key=lambda s: s[-1][1]) if opens else ()
    _CALENDAR[(currency, None)] = merged
    _CALENDAR_TZ[(currency, None)] = _CALENDAR_TZ[members[0][0]]


def note_venue_hours(currency: str | None, exchange: str | None, details) -> int:
    """Record the liquidHours of a ContractDetails (or [details]) under (currency, exchange).
    Returns the number of dates noted (0 when nothing usable)."""
    try:
        d = details[0] if isinstance(details, (list, tuple)) else details
        text = getattr(d, "liquidHours", "") or ""
        tz_name = getattr(d, "timeZoneId", "") or ""
        ccy = (currency or "").upper()
        if not text or not tz_name or not ccy:
            return 0
        parsed = parse_liquid_hours(text, tz_name)
        if not parsed:
            return 0
        key = (ccy, (exchange or "").upper() or None)
        _CALENDAR.setdefault(key, {}).update(parsed)
        _CALENDAR_TZ[key] = tz_name
        _merge_currency(ccy)
        return len(parsed)
    except Exception as e:
        log.warning("venue_hours_note_failed", currency=currency, exchange=exchange, error=str(e))
        return 0


def overlay_sessions(currency: str | None, now: datetime,
                     exchange: str | None = None) -> tuple[tuple[datetime, datetime], ...] | None:
    """The overlay's sessions for the venue-local day containing `now` — the exchange-specific entry
    when known, else the merged per-currency one. None when the overlay says nothing about that day."""
    ccy = (currency or "").upper()
    for key in (((ccy, (exchange or "").upper()) if exchange else None), (ccy, None)):
        if key is None or key not in _CALENDAR:
            continue
        try:
            local_day = now.astimezone(pytz.timezone(_CALENDAR_TZ[key])).strftime("%Y%m%d")
        except Exception:
            continue
        if local_day in _CALENDAR[key]:
            return _CALENDAR[key][local_day]
    return None


def sessions_for(currency: str | None, now: datetime,
                 exchange: str | None = None) -> tuple[tuple[datetime, datetime], ...] | None:
    """Overlay first, static table second. `now` must be tz-aware. None for an unmapped currency."""
    ov = overlay_sessions(currency, now, exchange)
    if ov is not None:
        return ov
    spec = MARKET_HOURS.get((currency or "").upper())
    if not spec:
        return None
    local = now.astimezone(pytz.timezone(spec[0]))
    return static_sessions(currency, local.date())


def calendar_age_s(now: datetime | None = None) -> float | None:
    if _LAST_REFRESH is None:
        return None
    return ((now or datetime.now(pytz.UTC)) - _LAST_REFRESH).total_seconds()


def refresh_calendar(ib, venues: Iterable[tuple[str, str, str]], lock=None, max_age_s: float = 6 * 3600,
                     contract_factory=None) -> int:
    """Pull liquidHours for one sample contract per (currency, exchange, symbol) and note them.
    Skips entirely while the last refresh is younger than `max_age_s`. Returns venues noted."""
    global _LAST_REFRESH
    age = calendar_age_s()
    if age is not None and age < max_age_s:
        return 0
    from src.portfolio.symbols import broker_stock
    make = contract_factory or broker_stock
    noted = 0
    for ccy, exch, sym in venues:
        try:
            contract = make(sym, exch or "SMART", ccy)
            if lock is not None:
                with lock:
                    details = ib.reqContractDetails(contract) or []
            else:
                details = ib.reqContractDetails(contract) or []
            if note_venue_hours(ccy, exch, details):
                noted += 1
        except Exception as e:
            log.warning("venue_hours_refresh_failed", currency=ccy, exchange=exch, symbol=sym, error=str(e))
    _LAST_REFRESH = datetime.now(pytz.UTC)
    if noted:
        closed = sorted(f"{k[0]}@{k[1]}:{d}" for k, cal in _CALENDAR.items() if k[1]
                        for d, s in cal.items() if not s and
                        datetime.strptime(d, "%Y%m%d").weekday() in MON_FRI)
        log.info("venue_hours_calendar_refreshed", venues=noted, weekday_closures=closed[:12])
    return noted


def reset_calendar() -> None:
    """Tests only."""
    global _LAST_REFRESH
    _CALENDAR.clear()
    _CALENDAR_TZ.clear()
    _LAST_REFRESH = None
