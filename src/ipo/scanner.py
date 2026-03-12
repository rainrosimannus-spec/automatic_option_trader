"""
IPO Date Scanner — checks Finnhub's free IPO calendar API for upcoming IPO dates.

Matches company names from the ipo_watchlist against Finnhub's IPO calendar.
When a match is found, updates the expected_date and sends an alert.

Requires a free Finnhub API key: https://finnhub.io/register
Set in settings.yaml under:
  finnhub:
    api_key: "your_key_here"

Or as environment variable: FINNHUB_API_KEY
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from src.core.database import get_db
from src.core.logger import get_logger
from src.ipo.models import IpoWatchlist

log = get_logger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


def get_finnhub_key() -> Optional[str]:
    """Get Finnhub API key from settings or environment."""
    # Try environment variable first
    key = os.environ.get("FINNHUB_API_KEY")
    if key:
        return key

    # Try settings.yaml
    try:
        from src.core.config import get_settings
        settings = get_settings()
        key = settings.raw.get("finnhub", {}).get("api_key")
        if key:
            return key
    except Exception:
        pass

    return None


def scan_ipo_calendar():
    """
    Check Finnhub IPO calendar for any companies on our watchlist.
    Scans next 90 days of upcoming IPOs.
    Called daily by scheduler.
    """
    api_key = get_finnhub_key()
    if not api_key:
        log.debug("ipo_date_scan_skipped", reason="no Finnhub API key configured")
        return

    # Get our watchlist companies
    with get_db() as db:
        watching = db.query(IpoWatchlist).filter(
            IpoWatchlist.status == "watching",
            IpoWatchlist.expected_date.is_(None),  # only check those without dates
        ).all()

    if not watching:
        return

    # Build search terms from company names and tickers
    search_terms = {}
    for ipo in watching:
        # Use lowercase company name words for fuzzy matching
        name_words = set(ipo.company_name.lower().split())
        # Remove common words that cause false matches
        noise = {"inc", "inc.", "corp", "corp.", "ltd", "ltd.", "co", "co.",
                 "the", "and", "of", "technologies", "technology", "systems",
                 "group", "holdings"}
        name_words -= noise
        search_terms[ipo.id] = {
            "ticker": ipo.expected_ticker.upper(),
            "name_words": name_words,
            "company_name": ipo.company_name.lower(),
            "ipo": ipo,
        }

    # Query Finnhub IPO calendar for next 90 days
    today = datetime.utcnow().strftime("%Y-%m-%d")
    end_date = (datetime.utcnow() + timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        url = f"{FINNHUB_BASE_URL}/calendar/ipo?from={today}&to={end_date}&token={api_key}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.error("ipo_finnhub_request_failed", error=str(e))
        return

    ipo_calendar = data.get("ipoCalendar", [])
    if not ipo_calendar:
        log.debug("ipo_finnhub_no_upcoming", from_date=today, to_date=end_date)
        return

    log.info("ipo_finnhub_calendar_fetched", count=len(ipo_calendar))

    # Match against our watchlist
    matches = []
    for cal_entry in ipo_calendar:
        cal_name = (cal_entry.get("name") or "").lower()
        cal_symbol = (cal_entry.get("symbol") or "").upper()
        cal_date = cal_entry.get("date")

        if not cal_date:
            continue

        for ipo_id, terms in search_terms.items():
            matched = False

            # Exact ticker match
            if cal_symbol and cal_symbol == terms["ticker"]:
                matched = True

            # Company name fuzzy match (at least 2 significant words match)
            if not matched and terms["name_words"]:
                cal_words = set(cal_name.split())
                common = terms["name_words"] & cal_words
                if len(common) >= 2:
                    matched = True
                # Also try if our company name is contained in the calendar name
                if not matched and terms["company_name"] in cal_name:
                    matched = True
                # Or calendar name contained in our company name
                if not matched and cal_name and cal_name in terms["company_name"]:
                    matched = True

            if matched:
                matches.append({
                    "ipo_id": ipo_id,
                    "ipo": terms["ipo"],
                    "cal_name": cal_entry.get("name", ""),
                    "cal_symbol": cal_symbol,
                    "cal_date": cal_date,
                    "cal_price_range": f"${cal_entry.get('priceRangeLow', '?')} - ${cal_entry.get('priceRangeHigh', '?')}",
                    "cal_shares": cal_entry.get("numberOfShares"),
                    "cal_exchange": cal_entry.get("exchange", ""),
                })

    if not matches:
        log.debug("ipo_finnhub_no_matches", watched=len(watching), calendar_entries=len(ipo_calendar))
        return

    # Update watchlist and send alerts
    for match in matches:
        ipo = match["ipo"]
        with get_db() as db:
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == match["ipo_id"]).first()
            if entry:
                entry.expected_date = match["cal_date"]
                # Update ticker if Finnhub provides one
                if match["cal_symbol"]:
                    entry.expected_ticker = match["cal_symbol"]
                # Auto-set lockup date = IPO date + 180 days if not already set
                if match.get("cal_date") and not entry.lockup_date:
                    from datetime import timedelta
                    try:
                        ipo_dt = datetime.strptime(match["cal_date"], "%Y-%m-%d")
                        entry.lockup_date = (ipo_dt + timedelta(days=180)).strftime("%Y-%m-%d")
                        entry.lockup_enabled = True
                        log.info("ipo_lockup_date_auto_set",
                                 ticker=entry.expected_ticker,
                                 lockup_date=entry.lockup_date)
                    except Exception as e:
                        log.warning("ipo_lockup_date_calc_failed", error=str(e))
                entry.updated_at = datetime.utcnow()
                if entry.notes:
                    entry.notes += f" | Finnhub: {match['cal_name']}, {match['cal_price_range']}"
                else:
                    entry.notes = f"Finnhub: {match['cal_name']}, {match['cal_price_range']}"

        log.info("ipo_date_found",
                 company=ipo.company_name,
                 ticker=match["cal_symbol"],
                 date=match["cal_date"],
                 price_range=match["cal_price_range"])

        # Send alert
        try:
            from src.core.alerts import get_alert_manager
            get_alert_manager().send(
                title=f"🚀 IPO Date Found: {ipo.company_name}",
                body=(
                    f"Company: {match['cal_name']}\n"
                    f"Ticker: {match['cal_symbol'] or ipo.expected_ticker}\n"
                    f"Date: {match['cal_date']}\n"
                    f"Price range: {match['cal_price_range']}\n"
                    f"Exchange: {match['cal_exchange']}\n"
                    f"\n⚠️ CHECK & CONFIRM settings on IPO Rider page!"
                ),
                priority="urgent",
                tags="ipo,warning",
            )
        except Exception as e:
            log.warning("ipo_alert_failed", error=str(e))

    log.info("ipo_date_scan_complete", matches=len(matches))
