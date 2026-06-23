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

Until the Flex creds are set and synced, Total Invested falls back to the live
IBKR account-summary cash (so a fresh account reflects its actual deposited cash,
not a stale constant). Withdrawals are tracked too: the portfolio sync keeps the
sign of each cash flow, so Total Invested = net deposits − withdrawals.
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


def parse_deposits_from_flex(xml_content: str, include_withdrawals: bool = False) -> List[Dict]:
    """Parse cash-flow rows from Flex XML.

    Default: deposits only (positive amounts) — the legacy behaviour the options
    performance chart relies on. With include_withdrawals=True, withdrawals
    (negative amounts) are returned too with their sign PRESERVED, so the caller
    can sum them into a NET invested-capital figure (deposits add, withdrawals
    deduct). `amount` from IBKR is already signed.
    """
    root = ET.fromstring(xml_content)
    rows = []

    for dw in root.iter("DepositWithdrawal"):
        amount = float(dw.get("amount", 0))
        if amount == 0:
            continue
        if amount < 0 and not include_withdrawals:
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

        default_note = "deposit" if amount > 0 else "withdrawal"
        notes = (dw.get("description") or dw.get("type") or default_note)[:200]
        rows.append({
            "date": date_str,
            "amount_original": amount,
            "currency": currency,
            "notes": notes,
        })

    log.info("flex_cashflows_parsed", count=len(rows), include_withdrawals=include_withdrawals)
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


def sync_injections_from_ibkr(
    account_id: str | None = None,
    flex_token: str | None = None,
    flex_query_id: str | None = None,
    include_withdrawals: bool = False,
) -> int:
    """
    Pull deposit history from IBKR Flex, convert to USD, upsert into DB.
    Returns count of new rows added.

    With no args this syncs the PORTFOLIO account from its Flex creds (unchanged
    behaviour). The option-trader account passes its own account + Flex creds via
    `sync_options_injections_from_ibkr()`.
    """
    from src.core.config import get_settings

    cfg = get_settings().portfolio
    if account_id is None:
        account_id = getattr(cfg, "ibkr_account", "") or None
    if flex_token is None:
        flex_token = getattr(cfg, "flex_token", None)
    if flex_query_id is None:
        flex_query_id = getattr(cfg, "flex_query_id", None)

    if not flex_token or not flex_query_id:
        raise ValueError(
            "flex_token and flex_query_id are not set in config/settings.yaml. "
            "See the docstring at the top of src/portfolio/capital_injections.py."
        )

    xml_content = fetch_flex_statement(flex_token, flex_query_id)
    deposits = parse_deposits_from_flex(xml_content, include_withdrawals=include_withdrawals)

    added = 0
    pending_bridge_bumps = []  # collect (amount_usd, account_id) tuples for Bridge benchmark
    with get_db() as db:
        # Flex is the authoritative cash-flow source: drop any provisional manual_bootstrap rows
        # for this account before upserting the real deposits, so a hand-seeded placeholder (used to
        # keep the return chart honest while Flex was unavailable) can never double-count alongside
        # the genuine Flex row.
        db.query(PortfolioCapitalInjection).filter(
            PortfolioCapitalInjection.account_id == account_id,
            PortfolioCapitalInjection.source == "manual_bootstrap",
        ).delete(synchronize_session=False)

        for dep in deposits:
            existing = db.query(PortfolioCapitalInjection).filter(
                PortfolioCapitalInjection.date == dep["date"],
                PortfolioCapitalInjection.currency == dep["currency"],
                PortfolioCapitalInjection.amount_original == dep["amount_original"],
                PortfolioCapitalInjection.account_id == account_id,
            ).first()
            if existing:
                continue

            rate = _get_fx_rate_to_usd(dep["currency"], dep["date"])
            amount_usd = dep["amount_original"] * rate
            inj = PortfolioCapitalInjection(
                date=dep["date"],
                amount_original=dep["amount_original"],
                currency=dep["currency"],
                eur_usd_rate=rate if dep["currency"] == "EUR" else None,
                amount_usd=amount_usd,
                notes=dep["notes"],
                source="ibkr_flex",
                account_id=account_id,
            )
            db.add(inj)
            added += 1
            pending_bridge_bumps.append((amount_usd, account_id))

        db.commit()

    # Bridge benchmark hook: fired AFTER the injection commit succeeds.
    # If the injection is for the configured Bridge source_account,
    # bump bridge_benchmark by the injection amount so that capital
    # deposits never trigger fake sweep events.
    if pending_bridge_bumps:
        try:
            from src.portfolio.bridge import bump_bridge_benchmark
            for amount_usd, acct in pending_bridge_bumps:
                bump_bridge_benchmark(amount_usd, acct or "")
        except Exception as e:
            log.warning("bridge_benchmark_hook_failed", error=str(e))

    log.info("injections_synced", added=added, account_id=account_id)
    return added


def sync_options_injections_from_ibkr() -> int:
    """
    Sync the OPTION-TRADER account's deposit history from its own Flex query.

    Reads `ibkr.flex_token` / `ibkr.flex_query_id` / `ibkr.account` (the dedicated
    options account, U25878705 after the 2026-06 split) and records each deposit as
    a PortfolioCapitalInjection tagged with that account_id. The performance chart
    then divides NLV by the cumulative deposits as of each date, so a fresh deposit
    raises NLV and invested capital together — the return % dilutes instead of
    spiking. Each new deposit also bumps the cash-bridge benchmark (via the shared
    sync path) so deposits never fire a fake NLV-doubling sweep.

    No-op (returns 0) until the options Flex creds are set in settings.yaml.
    """
    from src.core.config import get_settings

    ibkr = get_settings().ibkr
    token = getattr(ibkr, "flex_token", "") or ""
    query_id = getattr(ibkr, "flex_query_id", "") or ""
    if not token or not query_id:
        log.info("options_injection_sync_skipped", reason="flex creds unset")
        return 0

    return sync_injections_from_ibkr(
        account_id=ibkr.account or None,
        flex_token=token,
        flex_query_id=query_id,
    )


def get_total_invested_usd(account_id: str | None = None) -> float:
    """
    Net capital invested in USD for the given account (deposits − withdrawals),
    summed from the IBKR-Flex-sourced injections table.

    When no net-positive cash-flow rows exist yet (fresh account, or Flex not yet
    synced), the PORTFOLIO account falls back to the live account-summary cash
    (cached NLV) so the figure reflects the money actually in the account rather
    than a stale constant. For any other account_id (e.g. the bridge's options
    source_account) the fallback is 0.0, preserving prior behaviour.
    """
    try:
        with get_db() as db:
            q = db.query(PortfolioCapitalInjection)
            if account_id is not None:
                q = q.filter(PortfolioCapitalInjection.account_id == account_id)
            rows = q.all()
            if rows:
                total = sum(r.amount_usd for r in rows)
                if total > 0:
                    return total
    except Exception as e:
        log.warning("get_total_invested_usd_failed", error=str(e))

    # No (net-positive) cash-flow rows yet → portfolio falls back to account cash.
    try:
        from src.core.config import get_settings
        port_acct = getattr(get_settings().portfolio, "ibkr_account", "") or None
    except Exception:
        port_acct = None
    if account_id is None or account_id == port_acct:
        return _account_summary_cash_usd()
    return 0.0


def _account_summary_cash_usd() -> float:
    """Best-effort 'cash put in' from the IBKR portfolio account-summary cache.
    For a freshly-funded, un-invested account NLV ≈ deposited cash, so this is a
    sane Total-Invested bootstrap until Flex cash-flow rows are synced. Returns
    0.0 if the account cache is unavailable (e.g. gateway not yet connected)."""
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        acct = get_cached_portfolio_account() or {}
        nlv = float(acct.get("nlv") or 0.0)
        if nlv > 0:
            return nlv
    except Exception as e:
        log.warning("account_summary_cash_lookup_failed", error=str(e))
    return 0.0


def get_total_invested_base(account_id: str | None = None) -> float:
    """Net capital invested in the account's BASE currency (deposits − withdrawals), summed from the
    deposit ORIGINAL amounts — i.e. the currency the account was funded in (EUR for U26413485, a
    euro-base account). Use this for the EUR portfolio dashboard so Total Invested is shown in euros,
    not a USD-converted figure. Falls back to the account-summary cash (cached NLV, already in base
    currency) when no deposit rows exist yet; 0.0 for any non-portfolio account_id.

    NOTE: assumes deposits were funded in the account's base currency (the normal case). Mixed-currency
    funding would need per-row FX-at-deposit conversion — not handled here.
    """
    try:
        with get_db() as db:
            q = db.query(PortfolioCapitalInjection)
            if account_id is not None:
                q = q.filter(PortfolioCapitalInjection.account_id == account_id)
            rows = q.all()
            if rows:
                total = sum(r.amount_original for r in rows)
                if total > 0:
                    return total
    except Exception as e:
        log.warning("get_total_invested_base_failed", error=str(e))
    try:
        from src.core.config import get_settings
        port_acct = getattr(get_settings().portfolio, "ibkr_account", "") or None
    except Exception:
        port_acct = None
    if account_id is None or account_id == port_acct:
        return _account_summary_cash_usd()  # cached NLV is already in the account's base currency
    return 0.0


def get_capital_ledger_base(account_id: str | None = None) -> "list[tuple[str, float]]":
    """Cumulative net invested (base currency) as of each injection date, ascending.

    Returns [(date_str, cumulative_invested_base), ...] built from the deposit/withdrawal
    ledger for `account_id`, summing the ORIGINAL (base-currency) amounts. The performance
    chart uses this to measure each snapshot's return against the capital that was actually in
    the account ON that date — so a mid-series deposit raises NLV and the invested base
    together (the return dilutes) instead of showing up as growth. Same-day flows collapse to
    one cumulative point. Empty list when no ledger rows exist (the caller then falls back to a
    single current-invested scalar).
    """
    try:
        with get_db() as db:
            q = db.query(PortfolioCapitalInjection)
            if account_id is not None:
                q = q.filter(PortfolioCapitalInjection.account_id == account_id)
            rows = q.order_by(PortfolioCapitalInjection.date.asc()).all()
    except Exception as e:
        log.warning("get_capital_ledger_base_failed", error=str(e))
        return []

    ledger: "list[tuple[str, float]]" = []
    running = 0.0
    for r in rows:
        running += float(r.amount_original or 0.0)
        d = str(r.date)[:10]
        if ledger and ledger[-1][0] == d:
            ledger[-1] = (d, running)  # collapse same-day flows into one cumulative point
        else:
            ledger.append((d, running))
    return ledger


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
