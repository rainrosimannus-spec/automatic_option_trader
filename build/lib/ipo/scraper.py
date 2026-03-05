"""
IPO Date Scanner — scrapes public IPO calendars for companies on the IPO watchlist.

Sources checked (in order):
  1. Finnhub IPO Calendar API (free, no key needed for basic)
  2. stockanalysis.com/ipos/calendar/ (HTML scrape)
  3. nasdaq.com/market-activity/ipos (HTML scrape)

Runs daily. When a company from the watchlist is found with a confirmed date,
it updates the expected_date and sends an alert.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from src.core.database import get_db
from src.core.logger import get_logger
from src.ipo.models import IpoWatchlist

log = get_logger(__name__)

# User-Agent to avoid being blocked
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def scan_ipo_dates():
    """
    Main entry point — check all sources for IPO dates matching our watchlist.
    Updates expected_date if found and sends alerts.
    """
    # Get all watching IPOs without a date (or with a date far in future)
    with get_db() as db:
        watchlist = db.query(IpoWatchlist).filter(
            IpoWatchlist.status.in_(["watching", "lockup_waiting"]),
        ).all()

    if not watchlist:
        return

    # Build lookup: company name variations → IpoWatchlist id
    company_lookup = {}
    for ipo in watchlist:
        # Match by company name (case-insensitive, partial match)
        name_lower = ipo.company_name.lower().strip()
        company_lookup[name_lower] = ipo
        # Also match by ticker
        company_lookup[ipo.expected_ticker.lower()] = ipo
        # First word of company name (e.g. "SpaceX" from "SpaceX Inc.")
        first_word = name_lower.split()[0] if name_lower else ""
        if first_word and len(first_word) > 3:
            company_lookup[first_word] = ipo

    # Collect IPO listings from all sources
    found_ipos = []
    found_ipos.extend(_check_finnhub())
    found_ipos.extend(_check_stockanalysis())
    found_ipos.extend(_check_nasdaq())

    if not found_ipos:
        log.info("ipo_date_scan_done", found=0)
        return

    # Match against our watchlist
    matches = []
    for listing in found_ipos:
        listing_name = listing.get("name", "").lower()
        listing_ticker = listing.get("ticker", "").lower()
        listing_date = listing.get("date", "")

        if not listing_date:
            continue

        # Try to match
        matched_ipo = None
        for key, ipo in company_lookup.items():
            if key in listing_name or key == listing_ticker:
                matched_ipo = ipo
                break
            # Also check if listing name contains our company name
            if len(key) > 3 and key in listing_name:
                matched_ipo = ipo
                break

        if matched_ipo:
            matches.append({
                "ipo": matched_ipo,
                "found_name": listing.get("name", ""),
                "found_ticker": listing_ticker.upper(),
                "found_date": listing_date,
                "source": listing.get("source", "unknown"),
            })

    # Update dates and send alerts
    for match in matches:
        ipo = match["ipo"]
        new_date = match["found_date"]

        # Only update if date is different from what we have
        if ipo.expected_date == new_date:
            continue

        old_date = ipo.expected_date

        with get_db() as db:
            entry = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo.id).first()
            if entry:
                entry.expected_date = new_date
                # Also update ticker if we found a confirmed one
                if match["found_ticker"] and match["found_ticker"] != entry.expected_ticker:
                    entry.expected_ticker = match["found_ticker"]
                entry.updated_at = datetime.utcnow()

        log.info("ipo_date_found",
                 company=ipo.company_name,
                 ticker=match["found_ticker"],
                 date=new_date,
                 source=match["source"],
                 old_date=old_date)

        # Send alert
        _send_ipo_date_alert(ipo, match)

    log.info("ipo_date_scan_done", checked=len(found_ipos), matches=len(matches))


def _send_ipo_date_alert(ipo: IpoWatchlist, match: dict):
    """Send alert when IPO date is found or changed."""
    try:
        from src.core.alerts import get_alert_manager
        get_alert_manager().send(
            title=f"🚀 IPO Date Found: {ipo.company_name}",
            body=(
                f"Company: {match['found_name']}\n"
                f"Ticker: {match['found_ticker']}\n"
                f"Expected Date: {match['found_date']}\n"
                f"Source: {match['source']}\n"
                f"\n"
                f"⚠️ Review on IPO Rider page and confirm settings.\n"
                f"Scanner will activate 7 days before this date."
            ),
            priority="high",
            tags="ipo,calendar",
        )
    except Exception as e:
        log.warning("ipo_date_alert_failed", error=str(e))


def _check_finnhub() -> list[dict]:
    """Check Finnhub free IPO calendar API."""
    results = []
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        future = (datetime.utcnow() + timedelta(days=90)).strftime("%Y-%m-%d")

        url = f"https://finnhub.io/api/v1/calendar/ipo?from={today}&to={future}&token=demo"
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        for item in data.get("ipoCalendar", []):
            results.append({
                "name": item.get("name", ""),
                "ticker": item.get("symbol", ""),
                "date": item.get("date", ""),
                "source": "finnhub",
            })

        log.debug("finnhub_ipo_check", found=len(results))
    except Exception as e:
        log.debug("finnhub_ipo_check_failed", error=str(e))

    return results


def _check_stockanalysis() -> list[dict]:
    """Scrape stockanalysis.com IPO calendar."""
    results = []
    try:
        url = "https://stockanalysis.com/ipos/calendar/"
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode()

        # Look for IPO entries in the HTML
        # Pattern: table rows with company name, ticker, date
        # stockanalysis uses JSON embedded in script tags
        json_match = re.search(r'<script[^>]*>.*?"ipos"\s*:\s*(\[.*?\])', html, re.DOTALL)
        if json_match:
            try:
                ipos = json.loads(json_match.group(1))
                for item in ipos:
                    results.append({
                        "name": item.get("name", ""),
                        "ticker": item.get("symbol", item.get("ticker", "")),
                        "date": item.get("date", item.get("ipoDate", "")),
                        "source": "stockanalysis",
                    })
            except json.JSONDecodeError:
                pass

        # Fallback: simple regex for common patterns
        if not results:
            # Look for rows like: <td>Company Name</td><td>TICK</td><td>2026-03-15</td>
            rows = re.findall(
                r'<td[^>]*>([^<]+)</td>\s*<td[^>]*>([A-Z]{1,5})</td>\s*<td[^>]*>(\d{4}-\d{2}-\d{2})</td>',
                html,
            )
            for name, ticker, date in rows:
                results.append({
                    "name": name.strip(),
                    "ticker": ticker.strip(),
                    "date": date.strip(),
                    "source": "stockanalysis",
                })

        log.debug("stockanalysis_ipo_check", found=len(results))
    except Exception as e:
        log.debug("stockanalysis_ipo_check_failed", error=str(e))

    return results


def _check_nasdaq() -> list[dict]:
    """Scrape NASDAQ IPO calendar."""
    results = []
    try:
        url = "https://api.nasdaq.com/api/ipo/calendar?date=upcoming"
        req = Request(url, headers={
            "User-Agent": _UA,
            "Accept": "application/json",
        })
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        # Nasdaq API returns data in data.upcoming.rows
        rows = (data.get("data", {}).get("upcoming", {}).get("rows", [])
                or data.get("data", {}).get("priced", {}).get("rows", []))

        for row in rows:
            # Nasdaq uses different field names
            name = row.get("companyName", row.get("name", ""))
            ticker = row.get("proposedTickerSymbol", row.get("symbol", ""))
            date = row.get("expectedPriceDate", row.get("date", ""))

            # Normalize date format
            if date and "/" in date:
                try:
                    dt = datetime.strptime(date, "%m/%d/%Y")
                    date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

            if name:
                results.append({
                    "name": name,
                    "ticker": ticker,
                    "date": date,
                    "source": "nasdaq",
                })

        log.debug("nasdaq_ipo_check", found=len(results))
    except Exception as e:
        log.debug("nasdaq_ipo_check_failed", error=str(e))

    return results
