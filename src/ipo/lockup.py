"""Reliable IPO lock-up expiry resolution from SEC EDGAR prospectus filings.

Lock-ups are NOT uniformly 180 days (RDDT 90, STUB 90, CRCL 180), so the IPO+180d guess mistimes
the unlock for a large share of names — and the entire Phase-2 edge is timing the REAL unlock. So we
read the actual period straight from the IPO prospectus (424B4 / S-1) on SEC EDGAR and return a
confidence flag: the caller auto-trades only 'confirmed' dates and alerts on 'low'/estimated ones.

Plain HTTP (urllib) — no asyncio, no IBKR, no event loop.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import date, timedelta

from src.core.logger import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "automatic-option-trader ipo-lockup rain.rosimannus@gmail.com"}
_PLAUSIBLE = {90, 120, 150, 180, 210, 270, 360}     # standard US IPO lock-up lengths
_PROSPECTUS_FORMS = ("424B4", "424B1", "424B3", "S-1/A", "S-1")
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_ticker_cik: dict[str, str] | None = None

# Canonical lock-up phrasings, strongest first. We filter every candidate to _PLAUSIBLE so stray
# "14 days"/"30 days" clauses (notice periods, market stand-still, etc.) can't masquerade as the lock-up.
_CANON = re.compile(r"(\d{2,3})\s*days\s+after\s+the\s+date\s+of\s+(?:this|the)\s+(?:final\s+)?prospectus", re.I)
_PERIOD = re.compile(r"lock[\s\-]?up[^.]{0,80}?period\s+of\s+(\d{2,3})\s*days", re.I)
_NEAR = re.compile(r"lock[\s\-]?up", re.I)
_DAYS = re.compile(r"(\d{2,3})\s*days", re.I)


def _get(url: str, raw: bool = False, timeout: int = 20):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data)


def _cik_for(ticker: str) -> str | None:
    global _ticker_cik
    if _ticker_cik is None:
        try:
            tk = _get(_TICKER_MAP_URL)
            _ticker_cik = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in tk.values()}
        except Exception as e:
            log.warning("edgar_ticker_map_failed", error=str(e))
            _ticker_cik = {}
    return _ticker_cik.get((ticker or "").upper())


def extract_lockup_days(text: str) -> tuple[int | None, str]:
    """Return (days, confidence) from prospectus text. confidence ∈ {confirmed, low, none}.

    Pure function (no network) so it's unit-testable. Every candidate is constrained to _PLAUSIBLE,
    and within a tier the modal value wins, so a stray clause can't outvote the real lock-up."""
    def _modal(vals):
        vals = [v for v in vals if v in _PLAUSIBLE]
        return max(set(vals), key=vals.count) if vals else None

    d = _modal(int(x) for x in _CANON.findall(text))
    if d:
        return d, "confirmed"
    d = _modal(int(x) for x in _PERIOD.findall(text))
    if d:
        return d, "confirmed"
    near: list[int] = []
    for m in _NEAR.finditer(text):
        for x in _DAYS.findall(text[m.start(): m.start() + 400]):
            if int(x) in _PLAUSIBLE:
                near.append(int(x))
    d = _modal(near)
    if d:
        return d, "low"
    return None, "none"


def resolve_lockup(ticker: str, ipo_date: str) -> dict | None:
    """Resolve the lock-up expiry for `ticker` from its EDGAR prospectus.

    ipo_date: 'YYYY-MM-DD' (the lock-up clock starts at the prospectus/IPO date).
    Returns {end_date, days, confidence, source} or None if it can't be resolved.
    """
    try:
        cik = _cik_for(ticker)
        if not cik:
            log.info("lockup_no_cik", ticker=ticker)
            return None
        sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        pick = None
        for i, f in enumerate(forms):
            if f in _PROSPECTUS_FORMS:
                pick = i
                if f.startswith("424"):     # prefer the final prospectus over the S-1 draft
                    break
        if pick is None:
            log.info("lockup_no_prospectus", ticker=ticker)
            return None
        acc = recent["accessionNumber"][pick].replace("-", "")
        doc = recent["primaryDocument"][pick]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
        html = _get(url, raw=True, timeout=30).decode("utf-8", "ignore")
        text = re.sub(r"<[^>]+>", " ", html)
        days, conf = extract_lockup_days(text)
        if not days:
            log.info("lockup_period_not_found", ticker=ticker, form=forms[pick])
            return None
        end = (date.fromisoformat(ipo_date) + timedelta(days=days)).isoformat()
        log.info("lockup_resolved", ticker=ticker, days=days, end=end, confidence=conf, form=forms[pick])
        return {"end_date": end, "days": days, "confidence": conf, "source": forms[pick]}
    except Exception as e:
        log.warning("lockup_resolve_failed", ticker=ticker, error=str(e))
        return None
