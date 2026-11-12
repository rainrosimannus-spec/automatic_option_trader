"""
Archive IBKR Flex statements so an arbitrary date range can be reported on.

Why an archive at all: the Flex Web Service v3 API takes only a token and a
saved-query id — the date range is baked into the query definition on IBKR's
side, so "beginning of the account → today" cannot be asked for over the wire.
The answer is to accumulate statements locally and range-filter here.

Two ways in, both landing in the same tables:

    import_from_api(side)      fetch the configured (report) Flex query
    import_from_xml(side, xml) an XML statement downloaded from Account
                               Management, or replayed from data/flex_archive/

Both are idempotent — events dedupe on (account, kind, external_id), position
snapshots are replaced wholesale for the (account, report_date) they cover.

The Flex query on the IBKR side must include these sections (Activity Flex):

    • Trades                        (Execution level of detail)
    • Cash Transactions             dividends, withholding tax, interest, fees
    • Deposits & Withdrawals
    • Open Positions                (Summary level) — REQUIRED for as-of holdings
    • Net Asset Value / Equity Summary in Base   — optional, header totals
    • Transfers, Corporate Actions  — optional, but they move shares

This module deliberately does NOT reuse src/bookkeeping/flex_extract's
dataclasses: bookkeeping normalizes for double-entry journal posting and drops
open positions entirely. It DOES reuse that module's fetch, which is the
correct one-SendRequest-then-poll implementation.
"""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import func

from src.core.config import get_settings
from src.core.database import get_db
from src.core.logger import get_logger
from src.reports.models import (
    FlexEquitySummary, FlexEvent, FlexPositionSnapshot, FlexStatementFile,
)

log = get_logger(__name__)

ARCHIVE_DIR = Path("data/flex_archive")

# Cash-transaction types that are really deposits/withdrawals wearing another
# hat — IBKR reports funding both ways depending on the query configuration.
_FUNDING_TYPES = ("deposit", "withdrawal")


# ── small parsing helpers ────────────────────────────────────────────────

def _norm_date(s: str) -> str:
    """IBKR dates arrive as YYYYMMDD or YYYY-MM-DD, sometimes with a time."""
    if not s:
        return ""
    s = s.split(";")[0].split(" ")[0].strip()
    if len(s) == 8 and "-" not in s:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _f(a: Dict[str, str], *keys: str, default: float = 0.0) -> float:
    """First parseable float among `keys` (IBKR renames fields between
    statement types — e.g. netCash vs amount)."""
    for k in keys:
        v = a.get(k, "")
        if v in ("", None):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return default


def _s(a: Dict[str, str], *keys: str, default: str = "") -> str:
    for k in keys:
        v = a.get(k)
        if v:
            return v
    return default


def _strike(a: Dict[str, str]) -> Optional[float]:
    v = a.get("strike", "")
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


# ── parsed shapes ────────────────────────────────────────────────────────

@dataclass
class ParsedEvent:
    kind: str
    external_id: str
    event_date: str
    settle_date: str = ""
    event_type: str = ""
    symbol: str = ""
    description: str = ""
    asset_category: str = ""
    put_call: str = ""
    strike: Optional[float] = None
    expiry: str = ""
    quantity: float = 0.0
    price: float = 0.0
    proceeds: float = 0.0
    commission: float = 0.0
    tax: float = 0.0
    amount: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    currency: str = "USD"
    fx_rate_to_base: float = 1.0
    level_of_detail: str = ""
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedPosition:
    report_date: str
    symbol: str = ""
    description: str = ""
    asset_category: str = ""
    put_call: str = ""
    strike: Optional[float] = None
    expiry: str = ""
    con_id: str = ""
    quantity: float = 0.0
    multiplier: float = 1.0
    mark_price: float = 0.0
    position_value: float = 0.0
    cost_basis_price: float = 0.0
    cost_basis_money: float = 0.0
    unrealized_pnl: float = 0.0
    currency: str = "USD"
    fx_rate_to_base: float = 1.0
    level_of_detail: str = ""


@dataclass
class ParsedEquity:
    report_date: str
    currency: str = ""
    cash: float = 0.0
    stock: float = 0.0
    options: float = 0.0
    dividend_accruals: float = 0.0
    interest_accruals: float = 0.0
    total: float = 0.0


@dataclass
class ParsedStatement:
    account_id: str
    from_date: str
    to_date: str
    period: str = ""
    when_generated: str = ""
    query_name: str = ""
    events: List[ParsedEvent] = field(default_factory=list)
    positions: List[ParsedPosition] = field(default_factory=list)
    equity: List[ParsedEquity] = field(default_factory=list)


# ── parsing ──────────────────────────────────────────────────────────────

def parse_statement(xml_content: str) -> List[ParsedStatement]:
    """Parse a <FlexQueryResponse> into one ParsedStatement per <FlexStatement>.

    A query covering several accounts, or several periods, yields several
    statements; each is archived separately.
    """
    root = ET.fromstring(xml_content)
    query_name = root.get("queryName", "")
    out: List[ParsedStatement] = []

    stmts = list(root.iter("FlexStatement"))
    if not stmts:
        raise ValueError(
            "No <FlexStatement> in the XML. Is this a Flex statement, or an "
            "error response from IBKR?"
        )

    for st in stmts:
        a = st.attrib
        parsed = ParsedStatement(
            account_id=_s(a, "accountId"),
            from_date=_norm_date(_s(a, "fromDate", "period")),
            to_date=_norm_date(_s(a, "toDate")),
            period=_s(a, "period"),
            when_generated=_norm_date(_s(a, "whenGenerated")),
            query_name=query_name,
        )
        parsed.events.extend(_parse_trades(st))
        parsed.events.extend(_parse_cash(st))
        parsed.events.extend(_parse_deposits(st))
        parsed.events.extend(_parse_transfers(st))
        parsed.events.extend(_parse_corporate_actions(st))
        parsed.positions.extend(_parse_positions(st, parsed.to_date))
        parsed.equity.extend(_parse_equity(st))

        # A statement with no explicit fromDate (period-style queries) still
        # needs a range for the archive index — derive it from what's inside.
        if not parsed.from_date or not parsed.to_date:
            dates = sorted(e.event_date for e in parsed.events if e.event_date)
            if dates:
                parsed.from_date = parsed.from_date or dates[0]
                parsed.to_date = parsed.to_date or dates[-1]

        out.append(parsed)

    log.info(
        "flex_statement_parsed",
        statements=len(out),
        events=sum(len(s.events) for s in out),
        positions=sum(len(s.positions) for s in out),
    )
    return out


def _prefer_detail(rows: list, preferred: str, fallback: str) -> list:
    """Flex emits the same fact at several levels of detail when the query asks
    for more than one (Trade EXECUTION *and* ORDER; OpenPosition SUMMARY *and*
    LOT). Keeping both double-counts, so take the preferred level when present.
    Rows with no levelOfDetail at all are always kept."""
    levels = {(r.level_of_detail or "").upper() for r in rows}
    if preferred in levels and fallback in levels:
        return [r for r in rows
                if (r.level_of_detail or "").upper() != fallback]
    return rows


def _parse_trades(st: ET.Element) -> List[ParsedEvent]:
    rows: List[ParsedEvent] = []
    for t in st.iter("Trade"):
        a = t.attrib
        ext = _s(a, "transactionID", "tradeID", "ibExecID", "ibOrderID")
        if not ext:
            continue
        proceeds = _f(a, "proceeds")
        commission = _f(a, "ibCommission")
        rows.append(ParsedEvent(
            kind="trade",
            external_id=str(ext),
            event_date=_norm_date(_s(a, "tradeDate", "dateTime", "settleDateTarget")),
            settle_date=_norm_date(_s(a, "settleDateTarget", "settleDate")),
            event_type=(_s(a, "buySell") or "").upper(),
            symbol=_s(a, "symbol"),
            description=_s(a, "description"),
            asset_category=_s(a, "assetCategory"),
            put_call=_s(a, "putCall"),
            strike=_strike(a),
            expiry=_norm_date(_s(a, "expiry")),
            quantity=_f(a, "quantity"),
            price=_f(a, "tradePrice", "price"),
            proceeds=proceeds,
            commission=commission,
            tax=_f(a, "taxes"),
            amount=_f(a, "netCash", default=proceeds + commission),
            cost_basis=abs(_f(a, "cost", "costBasis")),
            realized_pnl=_f(a, "fifoPnlRealized"),
            currency=_s(a, "currency", default="USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", default=1.0) or 1.0,
            level_of_detail=_s(a, "levelOfDetail"),
            raw=dict(a),
        ))
    return _prefer_detail(rows, "EXECUTION", "ORDER")


def _parse_cash(st: ET.Element) -> List[ParsedEvent]:
    rows: List[ParsedEvent] = []
    for c in st.iter("CashTransaction"):
        a = c.attrib
        ext = _s(a, "transactionID", "actionID")
        ctype = _s(a, "type")
        if not ext:
            # No id at all → synthesize a stable one so re-import still dedupes.
            ext = _composite_id("cash", _norm_date(_s(a, "dateTime", "reportDate")),
                                ctype, _s(a, "symbol"), _s(a, "currency"),
                                _s(a, "amount"))
        # Funding booked as a CashTransaction belongs with the deposits so the
        # statement's cash-flow section doesn't split it in two.
        low = ctype.lower()
        kind = "deposit" if any(f in low for f in _FUNDING_TYPES) else "cash"
        amount = _f(a, "amount")
        rows.append(ParsedEvent(
            kind=kind,
            external_id=str(ext),
            event_date=_norm_date(_s(a, "settleDate", "dateTime", "reportDate")),
            settle_date=_norm_date(_s(a, "settleDate")),
            event_type=ctype,
            symbol=_s(a, "symbol"),
            description=_s(a, "description"),
            asset_category=_s(a, "assetCategory"),
            amount=amount,
            proceeds=amount,
            currency=_s(a, "currency", default="USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", default=1.0) or 1.0,
            level_of_detail=_s(a, "levelOfDetail"),
            raw=dict(a),
        ))
    return rows


def _parse_deposits(st: ET.Element) -> List[ParsedEvent]:
    rows: List[ParsedEvent] = []
    for d in st.iter("DepositWithdrawal"):
        a = d.attrib
        # DepositWithdrawal has no guaranteed unique id → composite key.
        ext = _s(a, "transactionID") or _composite_id(
            "dw", _norm_date(_s(a, "reportDate", "settleDate")),
            _s(a, "currency"), _s(a, "amount"),
        )
        amount = _f(a, "amount")
        rows.append(ParsedEvent(
            kind="deposit",
            external_id=str(ext),
            event_date=_norm_date(_s(a, "settleDate", "reportDate", "dateTime")),
            settle_date=_norm_date(_s(a, "settleDate")),
            event_type="Deposit" if amount >= 0 else "Withdrawal",
            description=_s(a, "description", "type"),
            amount=amount,
            proceeds=amount,
            currency=_s(a, "currency", default="USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", default=1.0) or 1.0,
            level_of_detail=_s(a, "levelOfDetail"),
            raw=dict(a),
        ))
    return rows


def _parse_transfers(st: ET.Element) -> List[ParsedEvent]:
    rows: List[ParsedEvent] = []
    for t in st.iter("Transfer"):
        a = t.attrib
        ext = _s(a, "transactionID") or _composite_id(
            "tr", _norm_date(_s(a, "date", "reportDate")), _s(a, "symbol"),
            _s(a, "quantity"),
        )
        rows.append(ParsedEvent(
            kind="transfer",
            external_id=str(ext),
            event_date=_norm_date(_s(a, "date", "reportDate", "settleDate")),
            event_type=_s(a, "type", "direction", default="Transfer"),
            symbol=_s(a, "symbol"),
            description=_s(a, "description"),
            asset_category=_s(a, "assetCategory"),
            quantity=_f(a, "quantity"),
            price=_f(a, "positionAmount", "cashTransfer"),
            amount=_f(a, "cashTransfer"),
            cost_basis=abs(_f(a, "transferPrice")) * abs(_f(a, "quantity")),
            currency=_s(a, "currency", default="USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", default=1.0) or 1.0,
            level_of_detail=_s(a, "levelOfDetail"),
            raw=dict(a),
        ))
    return rows


def _parse_corporate_actions(st: ET.Element) -> List[ParsedEvent]:
    rows: List[ParsedEvent] = []
    for c in st.iter("CorporateAction"):
        a = c.attrib
        ext = _s(a, "transactionID", "actionID") or _composite_id(
            "ca", _norm_date(_s(a, "dateTime", "reportDate")), _s(a, "symbol"),
            _s(a, "quantity"),
        )
        rows.append(ParsedEvent(
            kind="corporate_action",
            external_id=str(ext),
            event_date=_norm_date(_s(a, "dateTime", "reportDate")),
            event_type=_s(a, "type", default="Corporate Action"),
            symbol=_s(a, "symbol"),
            description=_s(a, "description", "actionDescription"),
            asset_category=_s(a, "assetCategory"),
            quantity=_f(a, "quantity"),
            proceeds=_f(a, "proceeds"),
            amount=_f(a, "proceeds"),
            realized_pnl=_f(a, "fifoPnlRealized"),
            currency=_s(a, "currency", default="USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", default=1.0) or 1.0,
            level_of_detail=_s(a, "levelOfDetail"),
            raw=dict(a),
        ))
    return rows


def _parse_positions(st: ET.Element, fallback_date: str) -> List[ParsedPosition]:
    rows: List[ParsedPosition] = []
    for p in st.iter("OpenPosition"):
        a = p.attrib
        rows.append(ParsedPosition(
            report_date=_norm_date(_s(a, "reportDate")) or fallback_date,
            symbol=_s(a, "symbol"),
            description=_s(a, "description"),
            asset_category=_s(a, "assetCategory"),
            put_call=_s(a, "putCall"),
            strike=_strike(a),
            expiry=_norm_date(_s(a, "expiry")),
            con_id=_s(a, "conid", "conId"),
            quantity=_f(a, "position"),
            multiplier=_f(a, "multiplier", default=1.0) or 1.0,
            mark_price=_f(a, "markPrice"),
            position_value=_f(a, "positionValue"),
            cost_basis_price=_f(a, "costBasisPrice", "openPrice"),
            cost_basis_money=_f(a, "costBasisMoney"),
            unrealized_pnl=_f(a, "fifoPnlUnrealized"),
            currency=_s(a, "currency", default="USD"),
            fx_rate_to_base=_f(a, "fxRateToBase", default=1.0) or 1.0,
            level_of_detail=_s(a, "levelOfDetail"),
        ))
    return _prefer_detail(rows, "SUMMARY", "LOT")


def _parse_equity(st: ET.Element) -> List[ParsedEquity]:
    rows: List[ParsedEquity] = []
    seen: set[str] = set()
    # Both element names occur depending on the query's "in Base" flag.
    for tag in ("EquitySummaryByReportDateInBase", "EquitySummaryInBase"):
        for e in st.iter(tag):
            a = e.attrib
            d = _norm_date(_s(a, "reportDate", "date"))
            if not d or d in seen:
                continue
            seen.add(d)
            rows.append(ParsedEquity(
                report_date=d,
                currency=_s(a, "currency", default=""),
                cash=_f(a, "cash", "cashLong"),
                stock=_f(a, "stock", "stockLong"),
                options=_f(a, "options", "optionsLong"),
                dividend_accruals=_f(a, "dividendAccruals"),
                interest_accruals=_f(a, "interestAccruals"),
                total=_f(a, "total"),
            ))
    return rows


def _composite_id(*parts: str) -> str:
    return ":".join(p.strip() for p in parts)


# ── persistence ──────────────────────────────────────────────────────────

def store_statement(
    side: str,
    parsed: ParsedStatement,
    xml_content: str,
    *,
    source: str = "api",
    filename: str = "",
) -> dict:
    """Write one parsed statement into the archive. Idempotent.

    Returns a summary dict: statement_id, events_added, events_updated,
    positions, equity_rows, already_imported.
    """
    from src.reports import SIDES
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")

    configured = _configured_account(side)
    # Importing one company's statement into the other's tab would silently
    # merge two sets of books. Refuse it — an operator picking the wrong tab is
    # a mistake worth failing loudly on.
    if parsed.account_id and configured and parsed.account_id != configured:
        raise ValueError(
            f"This statement is for IBKR account {parsed.account_id}, but the "
            f"{side} side is account {configured}. Import it on the other tab."
        )
    sha = hashlib.sha256(xml_content.encode("utf-8")).hexdigest()
    account_id = parsed.account_id or configured

    xml_path = _write_xml(side, account_id, parsed, sha, xml_content)

    with get_db() as db:
        existing = (
            db.query(FlexStatementFile)
            .filter(FlexStatementFile.side == side,
                    FlexStatementFile.account_id == account_id,
                    FlexStatementFile.sha256 == sha)
            .first()
        )
        if existing is not None:
            # Same bytes already archived. Still re-run the row merge below so a
            # half-finished earlier import converges, but report it as a repeat.
            stmt_row = existing
            repeat = True
        else:
            stmt_row = FlexStatementFile(
                side=side,
                account_id=account_id,
                from_date=parsed.from_date,
                to_date=parsed.to_date,
                period=parsed.period,
                when_generated=parsed.when_generated,
                query_name=parsed.query_name,
                source=source,
                filename=filename,
                xml_path=str(xml_path),
                sha256=sha,
            )
            db.add(stmt_row)
            db.flush()
            repeat = False

        added, updated = _merge_events(db, side, account_id, stmt_row.id, parsed.events)
        n_pos = _replace_positions(db, side, account_id, stmt_row.id, parsed.positions)
        n_eq = _merge_equity(db, side, account_id, stmt_row.id, parsed.equity)

        stmt_row.n_events = len(parsed.events)
        stmt_row.n_positions = n_pos
        stmt_id = stmt_row.id

    log.info(
        "flex_statement_stored",
        side=side, account=account_id, statement_id=stmt_id,
        from_date=parsed.from_date, to_date=parsed.to_date,
        added=added, updated=updated, positions=n_pos, repeat=repeat,
    )
    return {
        "statement_id": stmt_id,
        "account_id": account_id,
        "from_date": parsed.from_date,
        "to_date": parsed.to_date,
        "events_added": added,
        "events_updated": updated,
        "positions": n_pos,
        "equity_rows": n_eq,
        "already_imported": repeat,
    }


def _merge_events(db, side, account_id, statement_id, events) -> tuple[int, int]:
    """Insert new events; refresh existing ones in place. The natural key is
    (account, kind, external_id) — a later statement restating the same trade
    (settled price, final commission) should overwrite, not duplicate."""
    if not events:
        return 0, 0
    keys = {(e.kind, e.external_id) for e in events}
    ext_ids = sorted({e.external_id for e in events})
    kinds = sorted({e.kind for e in events})

    # Chunk the IN list — SQLite caps host parameters per statement.
    existing: dict[tuple[str, str], FlexEvent] = {}
    for i in range(0, len(ext_ids), 400):
        chunk = ext_ids[i:i + 400]
        for r in (db.query(FlexEvent)
                    .filter(FlexEvent.side == side,
                            FlexEvent.account_id == account_id,
                            FlexEvent.kind.in_(kinds),
                            FlexEvent.external_id.in_(chunk))
                    .all()):
            if (r.kind, r.external_id) in keys:
                existing[(r.kind, r.external_id)] = r

    added = updated = 0
    for e in events:
        row = existing.get((e.kind, e.external_id))
        if row is None:
            row = FlexEvent(side=side, account_id=account_id,
                            kind=e.kind, external_id=e.external_id)
            db.add(row)
            added += 1
        else:
            updated += 1
        row.statement_id = statement_id
        row.event_date = e.event_date
        row.settle_date = e.settle_date
        row.event_type = e.event_type
        row.symbol = e.symbol
        row.description = e.description[:255]
        row.asset_category = e.asset_category
        row.put_call = e.put_call
        row.strike = e.strike
        row.expiry = e.expiry
        row.quantity = e.quantity
        row.price = e.price
        row.proceeds = e.proceeds
        row.commission = e.commission
        row.tax = e.tax
        row.amount = e.amount
        row.cost_basis = e.cost_basis
        row.realized_pnl = e.realized_pnl
        row.currency = e.currency
        row.fx_rate_to_base = e.fx_rate_to_base
        row.level_of_detail = e.level_of_detail
        row.raw_json = json.dumps(e.raw, sort_keys=True)
        existing[(e.kind, e.external_id)] = row
    return added, updated


def _replace_positions(db, side, account_id, statement_id, positions) -> int:
    """Position snapshots are replaced wholesale per report_date — a re-import
    of the same day must not stack duplicate holdings."""
    if not positions:
        return 0
    dates = {p.report_date for p in positions if p.report_date}
    if dates:
        (db.query(FlexPositionSnapshot)
           .filter(FlexPositionSnapshot.side == side,
                   FlexPositionSnapshot.account_id == account_id,
                   FlexPositionSnapshot.report_date.in_(dates))
           .delete(synchronize_session=False))
    n = 0
    for p in positions:
        if not p.report_date:
            continue
        db.add(FlexPositionSnapshot(
            side=side, account_id=account_id, statement_id=statement_id,
            report_date=p.report_date, symbol=p.symbol,
            description=p.description[:255], asset_category=p.asset_category,
            put_call=p.put_call, strike=p.strike, expiry=p.expiry,
            con_id=p.con_id, quantity=p.quantity, multiplier=p.multiplier,
            mark_price=p.mark_price, position_value=p.position_value,
            cost_basis_price=p.cost_basis_price, cost_basis_money=p.cost_basis_money,
            unrealized_pnl=p.unrealized_pnl, currency=p.currency,
            fx_rate_to_base=p.fx_rate_to_base, level_of_detail=p.level_of_detail,
        ))
        n += 1
    return n


def _merge_equity(db, side, account_id, statement_id, equity) -> int:
    if not equity:
        return 0
    dates = {e.report_date for e in equity}
    existing = {
        r.report_date: r
        for r in db.query(FlexEquitySummary)
        .filter(FlexEquitySummary.side == side,
                FlexEquitySummary.account_id == account_id,
                FlexEquitySummary.report_date.in_(dates))
        .all()
    }
    for e in equity:
        row = existing.get(e.report_date)
        if row is None:
            row = FlexEquitySummary(side=side, account_id=account_id,
                                    report_date=e.report_date)
            db.add(row)
            existing[e.report_date] = row
        row.statement_id = statement_id
        row.currency = e.currency
        row.cash = e.cash
        row.stock = e.stock
        row.options = e.options
        row.dividend_accruals = e.dividend_accruals
        row.interest_accruals = e.interest_accruals
        row.total = e.total
    return len(equity)


def _write_xml(side, account_id, parsed, sha, xml_content) -> Path:
    """Keep the raw XML so the archive is rebuildable from source."""
    d = ARCHIVE_DIR / side
    d.mkdir(parents=True, exist_ok=True)
    name = f"{account_id or 'unknown'}_{parsed.from_date}_{parsed.to_date}_{sha[:12]}.xml"
    path = d / name
    if not path.exists():
        path.write_text(xml_content, encoding="utf-8")
    return path


# ── entry points ─────────────────────────────────────────────────────────

def _configured_account(side: str) -> str:
    cfg = get_settings()
    return (cfg.ibkr.account if side == "options" else cfg.portfolio.ibkr_account) or ""


def flex_credentials(side: str) -> tuple[str, str, str]:
    """(token, query_id, account_id) for a side.

    Prefers a dedicated long-period *report* query when one is configured
    (`ibkr.report_flex_query_id` / `portfolio.report_flex_query_id`), because
    the day-to-day query is short-period: the options one is shared with the
    SKXHoldco bookkeeping bridge and must stay as it is.
    """
    cfg = get_settings()
    src = cfg.ibkr if side == "options" else cfg.portfolio
    token = getattr(src, "flex_token", "") or ""
    query_id = (getattr(src, "report_flex_query_id", "") or
                getattr(src, "flex_query_id", "") or "")
    return token, query_id, _configured_account(side)


def import_from_api(side: str) -> dict:
    """Fetch the configured Flex query for `side` and archive every statement
    in it. Returns a summary dict."""
    from src.bookkeeping.flex_extract import fetch_flex_statement

    token, query_id, _ = flex_credentials(side)
    if not token or not query_id:
        raise ValueError(
            f"No Flex token/query configured for the {side} account. "
            f"Set {'ibkr' if side == 'options' else 'portfolio'}.flex_token and "
            f".report_flex_query_id in config/settings.yaml."
        )
    xml_content = fetch_flex_statement(token, query_id)
    return import_from_xml(side, xml_content, source="api")


def import_from_xml(side: str, xml_content: str, *, source: str = "upload",
                    filename: str = "") -> dict:
    """Archive every <FlexStatement> in an XML string."""
    statements = parse_statement(xml_content)
    results = [
        store_statement(side, p, xml_content, source=source, filename=filename)
        for p in statements
    ]
    return {
        "statements": len(results),
        "events_added": sum(r["events_added"] for r in results),
        "events_updated": sum(r["events_updated"] for r in results),
        "positions": sum(r["positions"] for r in results),
        "already_imported": all(r["already_imported"] for r in results) if results else False,
        "range": (
            min((r["from_date"] for r in results if r["from_date"]), default=""),
            max((r["to_date"] for r in results if r["to_date"]), default=""),
        ),
        "details": results,
    }


def reimport_archive(side: str) -> dict:
    """Re-parse every XML file on disk for a side. Useful after a parser change
    — the DB rows are derived data, the XML is the source."""
    d = ARCHIVE_DIR / side
    if not d.exists():
        return {"files": 0, "events_added": 0, "events_updated": 0}
    files = sorted(d.glob("*.xml"))
    added = updated = 0
    for p in files:
        try:
            r = import_from_xml(side, p.read_text(encoding="utf-8"),
                                source="upload", filename=p.name)
            added += r["events_added"]
            updated += r["events_updated"]
        except Exception as exc:
            log.warning("flex_reimport_failed", path=str(p), error=str(exc))
    return {"files": len(files), "events_added": added, "events_updated": updated}


# ── archive introspection (for the UI) ───────────────────────────────────

def archive_status(side: str) -> dict:
    """What the archive can currently answer for a side — drives the coverage
    banner and the date-picker bounds on /reports."""
    account_id = _configured_account(side)
    with get_db() as db:
        files = (
            db.query(FlexStatementFile)
            .filter(FlexStatementFile.side == side)
            .order_by(FlexStatementFile.to_date.desc(),
                      FlexStatementFile.imported_at.desc())
            .all()
        )
        n_events = (
            db.query(FlexEvent).filter(FlexEvent.side == side).count()
        )
        pos_dates = [
            r[0] for r in db.query(FlexPositionSnapshot.report_date)
            .filter(FlexPositionSnapshot.side == side)
            .distinct().order_by(FlexPositionSnapshot.report_date.desc()).all()
        ]
        ev_dates = db.query(
            func.min(FlexEvent.event_date), func.max(FlexEvent.event_date),
        ).filter(FlexEvent.side == side, FlexEvent.event_date != "").first()

        statements = [{
            "id": f.id,
            "from_date": f.from_date,
            "to_date": f.to_date,
            "period": f.period,
            "source": f.source,
            "filename": f.filename,
            "n_events": f.n_events,
            "n_positions": f.n_positions,
            "imported_at": f.imported_at,
        } for f in files]

    token, query_id, _ = flex_credentials(side)
    return {
        "side": side,
        "account_id": account_id,
        "statements": statements,
        "n_statements": len(statements),
        "n_events": n_events,
        "first_event": (ev_dates[0] if ev_dates else None) or "",
        "last_event": (ev_dates[1] if ev_dates else None) or "",
        "position_dates": pos_dates,
        "has_credentials": bool(token and query_id),
        "query_id": query_id,
    }


def earliest_activity(side: str) -> Optional[date]:
    """First archived event date for a side — the "from the beginning" bound."""
    with get_db() as db:
        row = (
            db.query(FlexEvent.event_date)
            .filter(FlexEvent.side == side, FlexEvent.event_date != "")
            .order_by(FlexEvent.event_date.asc())
            .first()
        )
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], "%Y-%m-%d").date()
    except ValueError:
        return None
