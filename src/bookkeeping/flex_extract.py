"""
Extract SKXHoldco's day from the IBKR Flex Web Service and normalize it into
typed events the translator can book.

Uses its OWN fetch (token → ReferenceCode → poll GetStatement) rather than
src.portfolio.capital_injections.fetch_flex_statement, because that helper (a)
treats the ErrorCode-1019 "statement generation in progress" *warning* as a
finished statement, and (b) re-issues the rate-limited SendRequest on every
retry — which trips IBKR's "Too many failed attempts" lockout. The correct
pattern is one SendRequest, then poll the cheap GetStatement until a real
<FlexQueryResponse> arrives (continuing on 1019, never re-SendRequest).

The Flex *query* on the IBKR side must include these sections (Activity Flex
Query):

    • Trades                — trades + commissions + realized P&L + FX (CASH)
    • Cash Transactions     — dividends, withholding tax, interest, fees
    • Deposits & Withdrawals

Amounts are kept in their original currency together with `fx_rate_to_base`
(IBKR's fxRateToBase), so the translator can post in the company base currency.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List

import requests

from src.core.logger import get_logger

log = get_logger(__name__)

_FLEX_SEND = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.SendRequest"
)
_FLEX_GET = (
    "https://gdcdyn.interactivebrokers.com"
    "/Universal/servlet/FlexStatementService.GetStatement"
)


def fetch_flex_statement(
    token: str,
    query_id: str,
    *,
    poll_attempts: int = 20,
    poll_interval: float = 6.0,
) -> str:
    """Request a Flex statement and return the <FlexQueryResponse> XML string.

    ONE SendRequest (the rate-limited op), then poll GetStatement. ErrorCode
    1019 ("generation in progress") keeps polling; any other error raises.
    """
    r = requests.get(_FLEX_SEND, params={"t": token, "q": query_id, "v": 3}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if root.findtext("Status") != "Success":
        raise RuntimeError(f"Flex SendRequest failed: {root.findtext('ErrorMessage') or r.text}")
    ref = root.findtext("ReferenceCode")
    log.info("flex_statement_requested", reference_code=ref)

    for attempt in range(poll_attempts):
        time.sleep(poll_interval)
        g = requests.get(_FLEX_GET, params={"t": token, "q": ref, "v": 3}, timeout=30)
        g.raise_for_status()
        txt = g.text
        if "<FlexQueryResponse" in txt:        # real data wrapper (not StatementResponse warning)
            log.info("flex_statement_ready", attempts=attempt + 1)
            return txt
        code = None
        try:
            code = ET.fromstring(txt).findtext("ErrorCode")
        except Exception:
            pass
        if code and code != "1019":
            raise RuntimeError(f"Flex GetStatement error {code}: {txt[:200]}")
        log.debug("flex_statement_pending", attempt=attempt, code=code)

    raise TimeoutError("Flex statement still generating after polling window")


def _norm_date(s: str) -> str:
    """IBKR dates come as YYYYMMDD or YYYY-MM-DD (sometimes with a time)."""
    if not s:
        return ""
    s = s.split(";")[0].split(" ")[0].strip()
    if len(s) == 8 and "-" not in s:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _f(attrib: Dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        v = attrib.get(key, "")
        return float(v) if v not in ("", None) else default
    except (TypeError, ValueError):
        return default


@dataclass
class TradeEvent:
    external_id: str          # tradeID / transactionID (idempotency key)
    date: str
    symbol: str
    asset_category: str       # STK, OPT, FOP, CASH (=FX), ...
    buy_sell: str             # BUY / SELL
    quantity: float
    proceeds: float           # signed: buy < 0, sell > 0 (excl. commission)
    commission: float         # signed, normally <= 0
    net_cash: float           # proceeds + commission (signed)
    cost_basis: float         # basis of the closed lot (sells); abs value
    realized_pnl: float       # fifoPnlRealized (gross of nothing — as IBKR gives)
    currency: str
    fx_rate_to_base: float
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def is_fx(self) -> bool:
        return self.asset_category.upper() == "CASH"


@dataclass
class CashEvent:
    external_id: str
    date: str
    type: str                 # raw IBKR "type" (Dividends, Withholding Tax, ...)
    symbol: str
    amount: float             # signed (in `currency`)
    currency: str
    fx_rate_to_base: float
    description: str
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class DepositEvent:
    external_id: str
    date: str
    amount: float             # signed: deposit > 0, withdrawal < 0
    currency: str
    fx_rate_to_base: float
    description: str
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class FlexDay:
    trades: List[TradeEvent] = field(default_factory=list)
    cash: List[CashEvent] = field(default_factory=list)
    deposits: List[DepositEvent] = field(default_factory=list)

    @property
    def total_events(self) -> int:
        return len(self.trades) + len(self.cash) + len(self.deposits)


# ── parsing ──────────────────────────────────────────────────────────────

def parse_flex(xml_content: str) -> FlexDay:
    """Parse a Flex statement XML string into normalized events."""
    root = ET.fromstring(xml_content)
    day = FlexDay()

    for t in root.iter("Trade"):
        a = t.attrib
        ext = a.get("tradeID") or a.get("transactionID") or a.get("ibOrderID") or ""
        day.trades.append(TradeEvent(
            external_id=str(ext),
            date=_norm_date(a.get("tradeDate") or a.get("dateTime") or a.get("settleDateTarget", "")),
            symbol=a.get("symbol", ""),
            asset_category=a.get("assetCategory", ""),
            buy_sell=(a.get("buySell") or "").upper(),
            quantity=_f(a, "quantity"),
            proceeds=_f(a, "proceeds"),
            commission=_f(a, "ibCommission"),
            net_cash=_f(a, "netCash", _f(a, "proceeds") + _f(a, "ibCommission")),
            cost_basis=abs(_f(a, "cost")),
            realized_pnl=_f(a, "fifoPnlRealized"),
            currency=a.get("currency", "USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", 1.0) or 1.0,
            raw=dict(a),
        ))

    for c in root.iter("CashTransaction"):
        a = c.attrib
        ext = a.get("transactionID") or a.get("actionID") or ""
        day.cash.append(CashEvent(
            external_id=str(ext),
            date=_norm_date(a.get("dateTime") or a.get("reportDate") or a.get("settleDate", "")),
            type=a.get("type", ""),
            symbol=a.get("symbol", ""),
            amount=_f(a, "amount"),
            currency=a.get("currency", "USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", 1.0) or 1.0,
            description=a.get("description", ""),
            raw=dict(a),
        ))

    for d in root.iter("DepositWithdrawal"):
        a = d.attrib
        # DepositWithdrawal has no guaranteed unique id → composite key
        ext = a.get("transactionID") or _composite_id(
            a.get("reportDate") or a.get("settleDate", ""),
            a.get("currency", ""),
            a.get("amount", ""),
        )
        day.deposits.append(DepositEvent(
            external_id=str(ext),
            date=_norm_date(a.get("reportDate") or a.get("settleDate") or a.get("dateTime", "")),
            amount=_f(a, "amount"),
            currency=a.get("currency", "USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", 1.0) or 1.0,
            description=a.get("description") or a.get("type", ""),
            raw=dict(a),
        ))

    log.info(
        "flex_parsed",
        trades=len(day.trades),
        cash=len(day.cash),
        deposits=len(day.deposits),
    )
    return day


def _composite_id(*parts: str) -> str:
    return "dw:" + "|".join(p.strip() for p in parts)


def extract_flex_day(token: str, query_id: str) -> FlexDay:
    """Fetch + parse the configured SKXHoldco Flex query."""
    if not token or not query_id:
        raise ValueError("bookkeeping.flex.token / query_id not set in settings.yaml")
    xml_content = fetch_flex_statement(token, query_id)
    return parse_flex(xml_content)
