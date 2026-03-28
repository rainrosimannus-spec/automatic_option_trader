"""
Fetch deposit/withdrawal history from IBKR via Flex Web Service.

One-time setup in IBKR Account Management:
  1. Reports → Flex Queries → Create → Activity Statement
  2. Tick "Deposits & Withdrawals" section, date range = "Since inception"
  3. Note the Query ID shown after saving
  4. Reports → Flex Web Service → Activate → note your token
  5. Add to config/settings.yaml under the portfolio: section:
       flex_token: "YOUR_TOKEN_HERE"
       flex_query_id: "YOUR_QUERY_ID_HERE"
  6. Then click Sync Deposits on the portfolio page

Until step 5 is done, the system uses the hardcoded seed of $498,514.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import List, Dict

import requests

from src.core.database import get_db
from src.core.logger import get_logger
from src.portfolio.models import PortfolioCapitalInjection

log = get_logger(__name__)

FLEX_REQUEST_URL = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.SendRequest"
)
FLEX_GET_URL = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.GetStatement"
)

SEED_USD = 498_514.0


def fetch_flex_statement(flex_token: str, query_id: str) -> str:
    """Request an IBKR Flex statement and return the XML string."""
    resp = requests.get(
        FLEX_REQUEST_URL,
        params={"t": flex_token, "q": query_id, "v": 3},
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    status = root.findtext("Status")
    if status != "Success":
        raise RuntimeError(
            f"Flex request failed: {root.findtext('ErrorMessage') or resp.text}"
        )

    reference_code = root.findtext("ReferenceCode")
    log.info("flex_statement_requested", reference_code=reference_code)

    for attempt in range(10):
        time.sleep(3)
        resp2 = requests.get(
            FLEX_GET_URL,
            params={"t": flex_token, "q": reference_code, "v": 3},
            timeout=30,
        )
        resp2.raise_for_status()

        if (
            "<FlexQueryResponse" in resp2.text
            or "<FlexStatementResponse" in resp2.text
        ):
            log.info("flex_statement_ready", attempts=attempt + 1)
            return resp2.text

        try:
            log.debug(
                "flex_statement_pending",
                status=ET.fromstring(resp2.text).findtext("Status"),
                attempt=attempt,
            )
        except Exception:
            pass

    raise TimeoutError("Flex statement not ready after 30 seconds")


def parse_deposits_from_flex(xml_content: str) -> List[Dict]:
    """Parse positive (deposit) rows from Flex XML."""
    root = ET.fromstring(xml_content)
    rows = []

    for dw in root.iter("DepositWithdrawal"):
        amount = float(dw.get("amount", 0))
        if amount <= 0:
            continue

        currency = dw.get("currency", "USD")
        date_str = (
            dw.get("reportDate")
            or dw.get("settleDate")
            or dw.get("date")
            or ""
        )
        if date_str and len(date_str) == 8 and "-" not in date_str:
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        notes = (dw.get("description") or dw.get("type") or "deposit")[:200]
        rows.append({
            "date": date_str,
            "amount_original": amount,
            "currency": currency,
            "notes": notes,
        })

    log.info("flex_deposits_parsed", count=len(rows))
    return rows


def _get_fx_rate_to_usd(currency: str, date_str: str) -> float:
    """Return USD per 1 unit of currency on date_str."""
    if currency == "USD":
        return 1.0

    try:
        from src.core.config import get_settings
        api_key = get_settings().fmp_api_key
        pair = f"{currency}USD"
        url = (
            f"https://financialmodelingprep.com/stable/historical-price-eod/full"
            f"?symbol={pair}&from={date_str}&to={date_str}&apikey={api_key}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, list) and data[0].get("close"):
            return float(data[0]["close"])
    except Exception as e:
        log.warning("fx_rate_fetch_failed", currency=currency, date=date_str, error=str(e))

    EUR_USD_AVGS = {
        "2022": 1.0533, "2023": 1.0821,
        "2024": 1.0814, "2025": 1.0490, "2026": 1.0600,
    }
    if currency == "EUR":
        year = date_str[:4] if date_str else "2026"
        return EUR_USD_AVGS.get(year, 1.0652)

    log.warning("no_fx_rate_fallback", currency=currency)
    return 1.0


def sync_injections_from_ibkr() -> int:
    """
    Pull deposit history from IBKR Flex, convert to USD, upsert into DB.
    Returns count of new rows added.
    """
    from src.core.config import get_settings

    cfg = get_settings().portfolio
    flex_token = getattr(cfg, "flex_token", None)
    flex_query_id = getattr(cfg, "flex_query_id", None)

    if not flex_token or not flex_query_id:
        raise ValueError(
            "flex_token and flex_query_id are not set in config/settings.yaml. "
            "See the docstring at the top of src/portfolio/capital_injections.py."
        )

    xml_content = fetch_flex_statement(flex_token, flex_query_id)
    deposits = parse_deposits_from_flex(xml_content)

    added = 0
    with get_db() as db:
        for dep in deposits:
            existing = db.query(PortfolioCapitalInjection).filter(
                PortfolioCapitalInjection.date == dep["date"],
                PortfolioCapitalInjection.currency == dep["currency"],
                PortfolioCapitalInjection.amount_original == dep["amount_original"],
            ).first()
            if existing:
                continue

            rate = _get_fx_rate_to_usd(dep["currency"], dep["date"])
            inj = PortfolioCapitalInjection(
                date=dep["date"],
                amount_original=dep["amount_original"],
                currency=dep["currency"],
                eur_usd_rate=rate if dep["currency"] == "EUR" else None,
                amount_usd=dep["amount_original"] * rate,
                notes=dep["notes"],
                source="ibkr_flex",
            )
            db.add(inj)
            added += 1

        db.commit()

    log.info("injections_synced", added=added)
    return added


def get_total_invested_usd() -> float:
    """
    Total capital injected in USD. Uses injections table or falls back to seed.
    """
    try:
        with get_db() as db:
            rows = db.query(PortfolioCapitalInjection).all()
            if rows:
                total = sum(r.amount_usd for r in rows)
                if total > 0:
                    return total
    except Exception as e:
        log.warning("get_total_invested_usd_failed", error=str(e))

    return SEED_USD


def fetch_accrued_interest_usd() -> float:
    """
    Fetch today's accrued interest from IBKR Flex Query.
    Returns the BASE_SUMMARY value (already in USD).
    """
    try:
        from src.core.config import get_settings
        cfg = get_settings()
        token = cfg.portfolio.flex_token
        qid = cfg.portfolio.flex_query_id
        if not token or not qid:
            return 0.0
        import xml.etree.ElementTree as ET
        xml_content = fetch_flex_statement(token, qid)
        root = ET.fromstring(xml_content)
        for el in root.iter("InterestAccrualsCurrency"):
            if el.attrib.get("currency") == "BASE_SUMMARY":
                return float(el.attrib.get("interestAccrued", 0))
    except Exception as e:
        log.warning("fetch_accrued_interest_failed", error=str(e))
    return 0.0


def fetch_dividends_ytd_usd() -> float:
    """
    Fetch dividends paid YTD from IBKR Flex Query.
    Uses ChangeInDividendAccrual entries with code='Re' (reversal = cash paid out).
    Returns sum of abs(netAmount) for current calendar year, in USD.
    """
    try:
        from src.core.config import get_settings
        from datetime import date
        cfg = get_settings()
        token = cfg.portfolio.flex_token
        qid = cfg.portfolio.flex_query_id
        if not token or not qid:
            return 0.0
        import xml.etree.ElementTree as ET
        xml_content = fetch_flex_statement(token, qid)
        root = ET.fromstring(xml_content)
        current_year = str(date.today().year)
        total = 0.0
        for el in root.iter("ChangeInDividendAccrual"):
            if el.attrib.get("code") == "Re":
                entry_date = el.attrib.get("date", "")
                if entry_date.startswith(current_year):
                    try:
                        fx = float(el.attrib.get("fxRateToBase", 1))
                        net = float(el.attrib.get("netAmount", 0))
                        total += abs(net) * fx
                    except Exception:
                        pass
        return total
    except Exception as e:
        log.warning("fetch_dividends_ytd_failed", error=str(e))
    return 0.0
