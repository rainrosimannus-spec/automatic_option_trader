"""Reliable IPO ticker + first-trading-day resolution from SEC EDGAR.

The ticker and IPO date used to come from a Finnhub demo key + stockanalysis/nasdaq HTML scrapes
(scraper.py) — unofficial, frequently stale or wrong. This resolves them from the authoritative
source instead, the same way lockup.py does for the lock-up period:

  - The final prospectus (424B4) is filed at pricing, on/just before the first trade. EDGAR's
    full-text search returns it with the company's ticker in `display_names` ("Reddit, Inc. (RDDT)
    (CIK ...)") and a `file_date` that IS the first trading day (RDDT 424B4 filed 2024-03-21, the
    day RDDT started trading) → confidence "confirmed".
  - Before pricing there's no 424B4, so we fall back to the S-1 / S-1/A registration statement and
    read the PROPOSED ticker out of the prospectus text ("under the symbol 'XYZ'"); no firm trading
    date is knowable yet → confidence "low" (the caller alerts rather than auto-arming on those).

Plain HTTP (urllib) — no asyncio, no IBKR, no event loop. Mirrors lockup.py's EDGAR conventions.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from src.core.logger import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "automatic-option-trader ipo-listing rain.rosimannus@gmail.com"}
_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
_FINAL_FORMS = ("424B4", "424B1")    # IPO final prospectus → confirmed ticker + date. NOT 424B3/424B5:
                                     # those are usually resale/secondary/shelf supplements filed AFTER
                                     # the IPO, so their file_date is not the first trading day.
_DRAFT_FORMS = ("S-1/A", "S-1")      # pre-pricing → proposed ticker, no firm date

# "Reddit, Inc.  (RDDT)  (CIK 0001713445)" → ticker, cik
_DISPLAY = re.compile(r"\(([A-Z][A-Z.\-]{0,6})\)\s*\(CIK\s*(\d{10})\)")
_CIK_ONLY = re.compile(r"\(CIK\s*(\d{10})\)")
# Prospectus text: 'under the symbol "XYZ"' / "symbol 'XYZ'" (straight or curly quotes)
_SYMBOL = re.compile(r"symbol[\s:]+[\"'“‘]\s*([A-Z][A-Z.\-]{0,6})\s*[\"'”’]")
_EXCHANGE = re.compile(r"(Nasdaq|New York Stock Exchange|NYSE American|NYSE)", re.I)


def _get(url: str, raw: bool = False, timeout: int = 25):
    # efts.sec.gov throws transient 500/503s — retry a few times with backoff before giving up.
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last


def _fts(company_name: str, forms: tuple[str, ...]) -> list[dict]:
    # NOTE: the query must NOT be wrapped in quotes — efts 500s on a quoted phrase. A plain term
    # search ranks the company's own filings first; we then match on display_names ourselves.
    q = urllib.parse.quote(company_name)
    url = f"{_FTS_URL}?q={q}&forms={','.join(forms)}"
    try:
        return _get(url).get("hits", {}).get("hits", [])
    except Exception as e:
        log.warning("ipo_fts_failed", company=company_name, error=str(e))
        return []


def _name_matches(query: str, display: str) -> bool:
    """Loose match: the query's significant tokens appear in the EDGAR display name."""
    q = re.sub(r"[^a-z0-9 ]", " ", query.lower())
    disp = re.sub(r"[^a-z0-9 ]", " ", display.lower())
    stop = {"inc", "corp", "corporation", "ltd", "limited", "holdings", "co", "company", "the", "plc", "group"}
    toks = [t for t in q.split() if t and t not in stop and len(t) > 2]
    if not toks:
        return False
    return all(t in disp for t in toks)


def _ticker_from_doc(cik: str, doc_id: str) -> tuple[str | None, str | None]:
    """Pull the proposed ticker + exchange out of the prospectus document text."""
    try:
        acc, _, doc = doc_id.partition(":")
        acc = acc.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
        html = _get(url, raw=True, timeout=30).decode("utf-8", "ignore")
        text = re.sub(r"<[^>]+>", " ", html)
        m = _SYMBOL.search(text)
        ticker = m.group(1) if m else None
        ex = _EXCHANGE.search(text)
        exchange = ex.group(1) if ex else None
        return ticker, exchange
    except Exception as e:
        log.warning("ipo_doc_parse_failed", cik=cik, error=str(e))
        return None, None


def resolve_listing(company_name: str) -> dict | None:
    """Resolve {ticker, exchange, ipo_date, first_trading_day, cik, confidence, source} for an IPO
    company from SEC EDGAR, or None if nothing is found.

    confidence ∈ {confirmed, low}: 'confirmed' = a final prospectus (424B4) exists, so the ticker is
    real and `first_trading_day` is its filing date; 'low' = only an S-1 draft (proposed ticker, no
    firm date yet — ipo_date/first_trading_day are None). The caller auto-arms only on 'confirmed'.
    """
    # 1) Final prospectus — authoritative ticker AND first trading day. A company can have several
    # final prospectuses over time (the IPO, then follow-ons); the IPO is the EARLIEST, so pick the
    # name-matched hit with the minimum file_date — a later follow-on 424B4 must not set the IPO date.
    finals = []
    for h in _fts(company_name, _FINAL_FORMS):
        s = h.get("_source", {})
        names = " ".join(s.get("display_names") or [])
        if _name_matches(company_name, names) and s.get("file_date"):
            finals.append((s["file_date"], names, s.get("file_type"), h.get("_id", "")))
    if finals:
        file_date, names, ftype, doc_id = min(finals, key=lambda x: x[0])
        m = _DISPLAY.search(names)
        cik = (m.group(2) if m else (_CIK_ONLY.search(names).group(1) if _CIK_ONLY.search(names) else None))
        ticker = m.group(1) if m else None
        if not ticker and cik:                      # ticker not in display name → read the prospectus
            ticker, _ = _ticker_from_doc(cik, doc_id)
        if ticker:
            log.info("ipo_listing_resolved", company=company_name, ticker=ticker,
                     first_trading_day=file_date, confidence="confirmed", source=ftype)
            return {"ticker": ticker, "exchange": None, "ipo_date": file_date,
                    "first_trading_day": file_date, "cik": cik,
                    "confidence": "confirmed", "source": ftype or "424B4"}

    # 2) Pre-pricing fallback — proposed ticker from the S-1, no firm date.
    for h in _fts(company_name, _DRAFT_FORMS):
        s = h.get("_source", {})
        names = " ".join(s.get("display_names") or [])
        if not _name_matches(company_name, names):
            continue
        cik_m = _CIK_ONLY.search(names)
        cik = cik_m.group(1) if cik_m else None
        if not cik:
            continue
        ticker, exchange = _ticker_from_doc(cik, h.get("_id", ""))
        if ticker:
            log.info("ipo_listing_proposed", company=company_name, ticker=ticker,
                     exchange=exchange, confidence="low", source=s.get("file_type"))
            return {"ticker": ticker, "exchange": exchange, "ipo_date": None,
                    "first_trading_day": None, "cik": cik,
                    "confidence": "low", "source": s.get("file_type") or "S-1"}

    log.info("ipo_listing_unresolved", company=company_name)
    return None


def sync_listings_from_sec() -> set[int]:
    """Authoritative pass over the watchlist: set ticker + first-trading-day from SEC EDGAR.

    Runs BEFORE the legacy Finnhub/scrape scan so SEC wins; returns the set of IpoWatchlist ids it
    resolved so the caller can skip them in the scrape fallback (which only fills what SEC couldn't).
    Only a 'confirmed' (424B4) result sets the firm expected_date + ticker_confirmed; a 'low' (S-1)
    result records the proposed ticker without a date and leaves the name for re-check / alerting.
    """
    from datetime import datetime
    from src.core.database import get_db
    from src.ipo.models import IpoWatchlist

    resolved: set[int] = set()
    with get_db() as db:
        watch = db.query(IpoWatchlist).filter(
            IpoWatchlist.status.in_(["watching", "lockup_waiting"]),
        ).all()
        rows = [(w.id, w.company_name, w.expected_ticker, w.expected_date) for w in watch]

    for wid, company, old_ticker, old_date in rows:
        try:
            r = resolve_listing(company)
        except Exception as e:
            log.warning("ipo_sec_sync_error", company=company, error=str(e))
            continue
        if not r:
            continue
        changed = False
        with get_db() as db:
            e = db.query(IpoWatchlist).filter(IpoWatchlist.id == wid).first()
            if not e:
                continue
            if r["ticker"] and r["ticker"] != e.expected_ticker:
                e.expected_ticker = r["ticker"]; changed = True
            e.date_source = r["source"]
            e.date_confidence = r["confidence"]
            if r["confidence"] == "confirmed":
                e.ticker_confirmed = True
                if r["first_trading_day"] and r["first_trading_day"] != e.expected_date:
                    e.expected_date = r["first_trading_day"]; changed = True
                resolved.add(wid)
            e.updated_at = datetime.utcnow()
        if changed:
            _alert_listing(company, r)
    log.info("ipo_sec_sync_done", resolved=len(resolved), checked=len(rows))
    return resolved


def _alert_listing(company: str, r: dict) -> None:
    try:
        from src.core.alerts import get_alert_manager
        conf = "✅ confirmed (424B4)" if r["confidence"] == "confirmed" else "⚠️ proposed (S-1, unconfirmed)"
        get_alert_manager().send(
            title=f"🚀 IPO listing resolved: {company}",
            body=(f"Ticker: {r['ticker']}\n"
                  f"First trading day: {r['first_trading_day'] or 'TBD (pre-pricing)'}\n"
                  f"Source: SEC EDGAR {r['source']} — {conf}\n"
                  f"\nReview on the IPO Rider page."),
            priority="high", tags="ipo,sec",
        )
    except Exception as e:
        log.warning("ipo_listing_alert_failed", error=str(e))
