"""
Account statement PDF — every transaction in a date range plus the state of
holdings on chosen dates.

Built entirely off the Flex archive (src/reports/flex_archive), never off the
internal ledger: `portfolio_transactions` holds buys only, and no internal table
records dividends, interest, fees or withholding tax at all. Anything the
archive doesn't cover is reported as a gap on the PDF rather than silently
omitted — a statement that quietly under-reports is worse than one that says
what it's missing.

Rendered with reportlab.platypus (pure Python, no system deps), landscape A4 so
the transaction table doesn't need to wrap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

from src.core.database import get_db
from src.core.logger import get_logger
from src.reports.flex_archive import _configured_account
from src.reports.models import (
    FlexEquitySummary, FlexEvent, FlexPositionSnapshot, FlexStatementFile,
)

log = get_logger(__name__)

STATEMENTS_DIR = Path("data/account_statements")

SIDE_LABELS = {
    "options": "Options trader (Maggy)",
    "portfolio": "Portfolio manager (Winston)",
}

# Order categories appear in the summary. "Trades" first because it's the bulk;
# the cash categories after it are the ones a tax return actually asks for.
CATEGORY_ORDER = [
    "Trades",
    "Dividends",
    "Withholding tax",
    "Interest",
    "Fees",
    "Other cash",
    "Corporate actions",
    "Transfers",
    "Deposits & withdrawals",
]


# ── data shapes ──────────────────────────────────────────────────────────

@dataclass
class TxnRow:
    event_date: str
    category: str
    event_type: str
    symbol: str
    description: str
    quantity: float
    price: float
    amount: float
    commission: float
    realized_pnl: float
    currency: str


@dataclass
class CategoryTotal:
    category: str
    currency: str
    count: int = 0
    amount: float = 0.0
    commission: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class HoldingRow:
    symbol: str
    description: str
    asset_category: str
    quantity: float
    cost_basis_price: float
    cost_basis_money: float
    mark_price: float
    position_value: float
    unrealized_pnl: float
    currency: str


@dataclass
class HoldingsBlock:
    requested_as_of: date
    actual_as_of: Optional[date]
    rows: list[HoldingRow] = field(default_factory=list)
    equity: Optional[dict] = None
    note: str = ""

    @property
    def is_exact(self) -> bool:
        return self.actual_as_of == self.requested_as_of


@dataclass
class StatementData:
    side: str
    side_label: str
    account_id: str
    period_start: date
    period_end: date
    generated_on: date
    transactions: list[TxnRow]
    totals: list[CategoryTotal]
    holdings: list[HoldingsBlock]
    coverage_gaps: list[tuple[date, date]]
    warnings: list[str]


# ── period presets ───────────────────────────────────────────────────────

def period_presets(today: Optional[date] = None) -> list[dict]:
    """The quick-pick ranges offered on /reports.

    "Last year" and "this year" are the two the request named; "all time" is
    the from-the-beginning case. Each carries the two default as-of dates for
    holdings: the 31.12. that closes the relevant tax year, and today.
    """
    today = today or date.today()
    ly = today.year - 1
    return [
        {
            "key": "last_year",
            "label": f"Last year ({ly})",
            "start": date(ly, 1, 1),
            "end": date(ly, 12, 31),
            "as_of": [date(ly - 1, 12, 31), date(ly, 12, 31)],
        },
        {
            "key": "this_year",
            "label": f"This year ({today.year})",
            "start": date(today.year, 1, 1),
            "end": today,
            "as_of": [date(ly, 12, 31), today],
        },
        {
            "key": "all_time",
            "label": "All time",
            "start": None,          # resolved to earliest archived activity
            "end": today,
            "as_of": [date(ly, 12, 31), today],
        },
    ]


def resolve_preset(side: str, key: str, today: Optional[date] = None) -> dict:
    """Expand a preset key into concrete dates, filling "all time"'s open start
    from the archive."""
    from src.reports.flex_archive import earliest_activity
    today = today or date.today()
    preset = next((p for p in period_presets(today) if p["key"] == key), None)
    if preset is None:
        raise ValueError(f"unknown preset {key!r}")
    preset = dict(preset)
    if preset["start"] is None:
        preset["start"] = earliest_activity(side) or date(today.year, 1, 1)
        # For all-time the useful opening snapshot is the year before the first
        # activity, not an arbitrary 31.12.
        preset["as_of"] = [date(today.year - 1, 12, 31), today]
    return preset


# ── collection ───────────────────────────────────────────────────────────

def _categorize(kind: str, event_type: str) -> str:
    if kind == "trade":
        return "Trades"
    if kind == "deposit":
        return "Deposits & withdrawals"
    if kind == "transfer":
        return "Transfers"
    if kind == "corporate_action":
        return "Corporate actions"
    t = (event_type or "").lower()
    if "withholding" in t:
        return "Withholding tax"
    if "dividend" in t or "payment in lieu" in t:
        return "Dividends"
    if "interest" in t:
        return "Interest"
    if "fee" in t or "commission" in t:
        return "Fees"
    return "Other cash"


def _d(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _coverage_gaps(side: str, start: date, end: date) -> list[tuple[date, date]]:
    """Days inside [start, end] that no archived statement covers.

    This is the honest half of the feature: the archive only knows what has
    been imported, and a range picker will happily ask for years that were
    never fetched.
    """
    with get_db() as db:
        rows = (
            db.query(FlexStatementFile.from_date, FlexStatementFile.to_date)
            .filter(FlexStatementFile.side == side)
            .all()
        )
    spans: list[tuple[date, date]] = []
    for f, t in rows:
        fd, td = _d(f), _d(t)
        if fd and td and td >= fd:
            spans.append((fd, td))
    if not spans:
        return [(start, end)]

    spans.sort()
    merged: list[list[date]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    gaps: list[tuple[date, date]] = []
    cursor = start
    for s, e in merged:
        if e < cursor:
            continue
        if s > end:
            break
        if s > cursor:
            gaps.append((cursor, min(s - timedelta(days=1), end)))
        cursor = max(cursor, e + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _holdings_block(side: str, account_id: str, as_of: date) -> HoldingsBlock:
    """Positions as reported on the latest snapshot date <= as_of.

    IBKR only reports open positions on statement dates, so an as-of date that
    falls on a weekend, a holiday, or simply outside any imported statement
    resolves backwards to the nearest one — labelled as such on the PDF.
    """
    as_of_s = as_of.isoformat()
    with get_db() as db:
        actual = (
            db.query(FlexPositionSnapshot.report_date)
            .filter(FlexPositionSnapshot.side == side,
                    FlexPositionSnapshot.report_date <= as_of_s,
                    FlexPositionSnapshot.report_date != "")
            .order_by(FlexPositionSnapshot.report_date.desc())
            .first()
        )
        if actual is None:
            return HoldingsBlock(
                requested_as_of=as_of, actual_as_of=None,
                note=("No open-position snapshot on or before this date. Import a "
                      "Flex statement whose Open Positions section covers it."),
            )
        actual_s = actual[0]
        pos = (
            db.query(FlexPositionSnapshot)
            .filter(FlexPositionSnapshot.side == side,
                    FlexPositionSnapshot.report_date == actual_s)
            .order_by(FlexPositionSnapshot.asset_category.asc(),
                      FlexPositionSnapshot.symbol.asc())
            .all()
        )
        eq = (
            db.query(FlexEquitySummary)
            .filter(FlexEquitySummary.side == side,
                    FlexEquitySummary.report_date == actual_s)
            .first()
        )
        rows = [HoldingRow(
            symbol=p.symbol,
            description=p.description or p.symbol,
            asset_category=p.asset_category,
            quantity=p.quantity or 0.0,
            cost_basis_price=p.cost_basis_price or 0.0,
            cost_basis_money=p.cost_basis_money or 0.0,
            mark_price=p.mark_price or 0.0,
            position_value=p.position_value or 0.0,
            unrealized_pnl=p.unrealized_pnl or 0.0,
            currency=p.currency or "",
        ) for p in pos]
        equity = None
        if eq is not None:
            equity = {
                "currency": eq.currency, "cash": eq.cash, "stock": eq.stock,
                "options": eq.options, "total": eq.total,
                "dividend_accruals": eq.dividend_accruals,
                "interest_accruals": eq.interest_accruals,
            }

    block = HoldingsBlock(requested_as_of=as_of, actual_as_of=_d(actual_s),
                          rows=rows, equity=equity)
    if not block.is_exact:
        block.note = (
            f"No statement closes exactly on {as_of.strftime('%d.%m.%Y')}; "
            f"showing the nearest earlier snapshot."
        )
    return block


def collect(side: str, start: date, end: date,
            as_of_dates: Iterable[date]) -> StatementData:
    """Assemble everything the PDF needs for one account and one range."""
    from src.reports import SIDES
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if end < start:
        raise ValueError("end date is before start date")

    account_id = _configured_account(side)
    start_s, end_s = start.isoformat(), end.isoformat()

    with get_db() as db:
        events = (
            db.query(FlexEvent)
            .filter(FlexEvent.side == side,
                    FlexEvent.event_date >= start_s,
                    FlexEvent.event_date <= end_s)
            .order_by(FlexEvent.event_date.asc(), FlexEvent.id.asc())
            .all()
        )
        txns = [TxnRow(
            event_date=e.event_date,
            category=_categorize(e.kind, e.event_type),
            event_type=e.event_type or e.kind,
            symbol=e.symbol or "",
            description=e.description or "",
            quantity=e.quantity or 0.0,
            price=e.price or 0.0,
            amount=e.amount or 0.0,
            commission=e.commission or 0.0,
            realized_pnl=e.realized_pnl or 0.0,
            currency=e.currency or "",
        ) for e in events]

    buckets: dict[tuple[str, str], CategoryTotal] = {}
    for t in txns:
        key = (t.category, t.currency)
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = CategoryTotal(category=t.category, currency=t.currency)
        b.count += 1
        b.amount += t.amount
        b.commission += t.commission
        b.realized_pnl += t.realized_pnl

    def _order(b: CategoryTotal) -> tuple[int, str]:
        idx = CATEGORY_ORDER.index(b.category) if b.category in CATEGORY_ORDER else 99
        return idx, b.currency
    totals = sorted(buckets.values(), key=_order)

    holdings = [_holdings_block(side, account_id, d) for d in as_of_dates]
    gaps = _coverage_gaps(side, start, end)

    warnings: list[str] = []
    if gaps:
        warnings.append(
            "The Flex archive does not cover the whole period — transactions in "
            "the gap(s) listed below are missing from this statement."
        )
    if not txns and not gaps:
        warnings.append("No transactions in this period.")

    return StatementData(
        side=side,
        side_label=SIDE_LABELS.get(side, side),
        account_id=account_id,
        period_start=start,
        period_end=end,
        generated_on=date.today(),
        transactions=txns,
        totals=totals,
        holdings=holdings,
        coverage_gaps=gaps,
        warnings=warnings,
    )


# ── rendering ────────────────────────────────────────────────────────────

def _money(v: float, dp: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{dp}f}"


def _qty(v: float) -> str:
    if not v:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _fmt_date(s: str) -> str:
    d = _d(s)
    return d.strftime("%d.%m.%Y") if d else (s or "—")


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


_HEAD_BG = colors.HexColor("#222222")
_ALT_BG = colors.HexColor("#f4f4f4")
_TOTAL_BG = colors.HexColor("#e8e8e8")
_RULE = colors.HexColor("#999999")


def _table_style(n_rows: int, right_from: int, *, total_row: bool = False) -> TableStyle:
    cmds = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ALIGN", (right_from, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _HEAD_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    last_body = n_rows - 2 if total_row else n_rows - 1
    for r in range(1, last_body + 1):
        if r % 2 == 0:
            cmds.append(("BACKGROUND", (0, r), (-1, r), _ALT_BG))
    if total_row:
        cmds += [
            ("BACKGROUND", (0, -1), (-1, -1), _TOTAL_BG),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, _RULE),
        ]
    return TableStyle(cmds)


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontSize=17, spaceAfter=4),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontSize=11, spaceBefore=8,
                             spaceAfter=4, textColor=colors.HexColor("#333333")),
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontSize=8.5, leading=11),
        "small": ParagraphStyle("Small", parent=base["BodyText"], fontSize=7.5,
                                leading=10, textColor=colors.HexColor("#666666")),
        "warn": ParagraphStyle("Warn", parent=base["BodyText"], fontSize=8, leading=11,
                               textColor=colors.HexColor("#9a3412")),
        "cell": ParagraphStyle("Cell", parent=base["BodyText"], fontSize=7, leading=8.5),
    }


def _page_furniture(data: StatementData):
    """Footer on every page: account, period and page number."""
    label = (f"{data.side_label} · {data.account_id} · "
             f"{data.period_start.strftime('%d.%m.%Y')}–{data.period_end.strftime('%d.%m.%Y')}")

    def _draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(15 * mm, 10 * mm, label)
        canvas.drawRightString(doc.pagesize[0] - 15 * mm, 10 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()
    return _draw


def _summary_section(data: StatementData, s: dict) -> list:
    story = [Paragraph("Summary by category", s["h2"])]
    if not data.totals:
        story.append(Paragraph("No transactions in this period.", s["body"]))
        return story

    rows = [["Category", "Ccy", "Count", "Net amount", "Commission", "Realized P&L"]]
    for t in data.totals:
        rows.append([
            t.category, t.currency, f"{t.count:,}",
            _money(t.amount),
            _money(t.commission) if t.commission else "—",
            _money(t.realized_pnl) if t.realized_pnl else "—",
        ])
    table = Table(rows, repeatRows=1, hAlign="LEFT",
                  colWidths=[55 * mm, 14 * mm, 18 * mm, 30 * mm, 26 * mm, 28 * mm])
    table.setStyle(_table_style(len(rows), right_from=2))
    story.append(table)
    story.append(Paragraph(
        "Amounts are in the transaction's own currency and are not converted. "
        "Net amount is the cash effect of the category (proceeds net of commission "
        "for trades).", s["small"]))
    return story


def _transactions_section(data: StatementData, s: dict) -> list:
    story = [Paragraph(
        f"Transactions — {len(data.transactions):,} row(s)", s["h2"])]
    if not data.transactions:
        story.append(Paragraph("No transactions in this period.", s["body"]))
        return story

    header = ["Date", "Category", "Type", "Symbol", "Description",
              "Quantity", "Price", "Amount", "Comm.", "Ccy"]
    rows = [header]
    for t in data.transactions:
        rows.append([
            _fmt_date(t.event_date),
            _trunc(t.category, 22),
            _trunc(t.event_type, 20),
            # Option symbols ("LRCX 251219C00400000") are far wider than the
            # column — a Paragraph wraps, a plain string overflows the cell.
            Paragraph(_trunc(t.symbol, 24), s["cell"]),
            Paragraph(_trunc(t.description, 90), s["cell"]),
            _qty(t.quantity),
            _money(t.price, 4) if t.price else "—",
            _money(t.amount),
            _money(t.commission) if t.commission else "—",
            t.currency,
        ])
    table = Table(
        rows, repeatRows=1, hAlign="LEFT",
        colWidths=[18 * mm, 28 * mm, 24 * mm, 20 * mm, 60 * mm,
                   20 * mm, 22 * mm, 26 * mm, 18 * mm, 11 * mm],
    )
    table.setStyle(_table_style(len(rows), right_from=5))
    story.append(table)
    return story


def _holdings_section(block: HoldingsBlock, s: dict) -> list:
    title = f"Holdings as of {block.requested_as_of.strftime('%d.%m.%Y')}"
    story = [Paragraph(title, s["h2"])]

    if block.actual_as_of is None:
        story.append(Paragraph(block.note, s["warn"]))
        return story
    if not block.is_exact:
        story.append(Paragraph(
            f"{block.note} Positions below are as reported on "
            f"<b>{block.actual_as_of.strftime('%d.%m.%Y')}</b>.", s["warn"]))

    if block.equity:
        eq = block.equity
        ccy = eq.get("currency") or "base ccy"
        story.append(Paragraph(
            f"Account totals ({ccy}): cash {_money(eq['cash'])} · "
            f"stock {_money(eq['stock'])} · options {_money(eq['options'])} · "
            f"<b>net asset value {_money(eq['total'])}</b>", s["body"]))

    if not block.rows:
        story.append(Paragraph("No open positions on this date.", s["body"]))
        return story

    header = ["Symbol", "Description", "Class", "Quantity", "Cost/unit",
              "Cost basis", "Mark", "Market value", "Unrealized P&L", "Ccy"]
    rows = [header]
    total_value = total_cost = total_pnl = 0.0
    currencies = set()
    for h in block.rows:
        rows.append([
            Paragraph(_trunc(h.symbol, 26), s["cell"]),
            Paragraph(_trunc(h.description, 70), s["cell"]),
            h.asset_category,
            _qty(h.quantity),
            _money(h.cost_basis_price, 4),
            _money(h.cost_basis_money),
            _money(h.mark_price, 4),
            _money(h.position_value),
            _money(h.unrealized_pnl),
            h.currency,
        ])
        total_value += h.position_value or 0.0
        total_cost += h.cost_basis_money or 0.0
        total_pnl += h.unrealized_pnl or 0.0
        if h.currency:
            currencies.add(h.currency)

    mixed = len(currencies) > 1
    rows.append([
        "Total", "" if not mixed else Paragraph(
            "<i>mixed currencies — totals are a raw sum, not FX-converted</i>", s["cell"]),
        "", "", "", _money(total_cost), "", _money(total_value), _money(total_pnl),
        "" if mixed else (next(iter(currencies), "")),
    ])

    table = Table(
        rows, repeatRows=1, hAlign="LEFT",
        colWidths=[22 * mm, 58 * mm, 14 * mm, 22 * mm, 22 * mm,
                   28 * mm, 22 * mm, 30 * mm, 28 * mm, 11 * mm],
    )
    table.setStyle(_table_style(len(rows), right_from=3, total_row=True))
    story.append(table)
    return story


def render_pdf(data: StatementData, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    s = _styles()

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=14 * mm, bottomMargin=16 * mm,
        title=(f"Account statement {data.account_id} "
               f"{data.period_start.isoformat()}–{data.period_end.isoformat()}"),
        author="Maggy & Winston",
    )

    story: list = [
        Paragraph("Account statement", s["h1"]),
        Paragraph(
            f"{data.side_label} &nbsp;·&nbsp; IBKR account <b>{data.account_id}</b>", s["body"]),
        Paragraph(
            f"Period: <b>{data.period_start.strftime('%d.%m.%Y')} – "
            f"{data.period_end.strftime('%d.%m.%Y')}</b> &nbsp;·&nbsp; "
            f"generated {data.generated_on.strftime('%d.%m.%Y')}", s["body"]),
        Spacer(1, 4),
    ]

    for w in data.warnings:
        story.append(Paragraph(w, s["warn"]))
    if data.coverage_gaps:
        gap_txt = ", ".join(
            f"{a.strftime('%d.%m.%Y')}–{b.strftime('%d.%m.%Y')}"
            for a, b in data.coverage_gaps)
        story.append(Paragraph(f"Uncovered: {gap_txt}", s["warn"]))
    if data.warnings or data.coverage_gaps:
        story.append(Spacer(1, 4))

    story += _summary_section(data, s)
    story.append(Spacer(1, 6))
    story += _transactions_section(data, s)

    for block in data.holdings:
        story.append(PageBreak())
        story += _holdings_section(block, s)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Source: IBKR Flex activity statements archived by Maggy &amp; Winston. "
        "Amounts are as reported by Interactive Brokers in the original transaction "
        "currency. This document is generated for record-keeping and tax-preparation "
        "purposes and is not a broker-issued statement.", s["small"]))

    draw = _page_furniture(data)
    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    log.info("account_statement_rendered", side=data.side, path=str(out_path),
             transactions=len(data.transactions))
    return out_path


def generate(side: str, start: date, end: date, as_of_dates: Iterable[date],
             storage_dir: Path | str | None = None) -> tuple[Path, StatementData]:
    """Collect + render in one call. Returns (pdf path, the data behind it).

    `storage_dir` resolves at call time, not at import time — binding
    STATEMENTS_DIR as a default value would make the module constant
    unoverridable, which is how a test run ends up writing into data/.
    """
    as_of_dates = list(as_of_dates)
    data = collect(side, start, end, as_of_dates)
    name = f"{side}_{data.account_id or 'account'}_{start.isoformat()}_{end.isoformat()}.pdf"
    out = Path(storage_dir if storage_dir is not None else STATEMENTS_DIR) / side / name
    render_pdf(data, out)
    return out, data
