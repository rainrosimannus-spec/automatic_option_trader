"""
Quarterly lender statement PDF generation.

Per docs/governance.md §5.8: a quarterly job emits per-lender PDFs summarizing
each loan's opening / movements / closing / accrued interest for the quarter.
PDFs are stored under data/statements/{counterparty_id}/ and surfaced in the
lender portal at /lenders/statements.

Lender-facing copy here is subject to LEGAL_CONTEXT.md §1-2 (no
deposit/savings/account/balance/fund/pool/investment terminology). Strings are
defined as constants at the top so the banned-terminology lint can scan this
file once it's added to that lint's scope.

PDFs are generated with reportlab.platypus — pure Python, no system deps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from src.borrower.accrual import compute_accrual
from src.borrower.models import (
    Counterparty, Loan, LoanStatus, MovementType, get_session_factory,
)


# --- Lender-facing strings (LEGAL_CONTEXT.md §1-2) -------------------------
TITLE = "Loan statement"
SUBTITLE_FMT = "{year} Q{quarter} — {counterparty_name}"
HEADER_COL_LOAN = "Loan"
HEADER_COL_OPENING = "Opening principal"
HEADER_COL_DISB = "Disbursed"
HEADER_COL_REPAY = "Repaid"
HEADER_COL_RESTR = "Restructure"
HEADER_COL_CLOSING = "Closing principal"
HEADER_COL_ACCRUED_Q = "Interest in quarter"
HEADER_COL_ACCRUED_CUM = "Accrued (cumulative)"
LABEL_GENERATED_BY = "Computed by Bruno on {date}."
LABEL_CONTACT = "For questions, contact rain@mesicap.com."
LABEL_DISCLAIMER = (
    "This statement is informational and does not modify the terms of your signed "
    "loan agreement. For binding terms, refer to your contract."
)
ISSUER_HEADER = "MesiCap Technologies OÜ · Estonia"

STATEMENTS_DIR = Path("data/statements")


# --- Data shape ------------------------------------------------------------

@dataclass
class LoanRow:
    loan_id: int
    description: str
    currency: str
    opening: float
    disbursed: float
    repaid: float
    restructure: float
    closing: float
    accrued_in_qtr: float
    accrued_cumulative: float


@dataclass
class StatementData:
    counterparty_id: int
    counterparty_name: str
    year: int
    quarter: int
    period_start: date
    period_end: date
    generated_on: date
    loans: list[LoanRow]


# --- Helpers ---------------------------------------------------------------

def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    start_month = 3 * (quarter - 1) + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    last_day = 31 if end_month in (3, 5, 7, 8, 10, 12) else 30
    if end_month == 2:
        last_day = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    return start, date(year, end_month, last_day)


def _outstanding_as_of(loan: Loan, as_of: date) -> float:
    out = 0.0
    for m in loan.movements:
        if m.movement_date > as_of:
            continue
        if m.movement_type == MovementType.DISBURSEMENT:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            out -= m.amount
    return out


def _movements_in(loan: Loan, start: date, end: date, mtype: MovementType) -> float:
    return sum(m.amount for m in loan.movements
               if mtype == m.movement_type and start <= m.movement_date <= end)


def collect_statement_data(counterparty_id: int, year: int, quarter: int) -> Optional[StatementData]:
    """Pull all the numbers needed for a lender's quarterly statement.

    Returns None if the counterparty has no loans relevant to the quarter
    (no active or recently-active loans). Returns a populated StatementData
    otherwise.
    """
    period_start, period_end = _quarter_bounds(year, quarter)
    day_before = period_start - timedelta(days=1)

    session = get_session_factory()()
    try:
        cp = session.query(Counterparty).filter_by(id=counterparty_id).first()
        if cp is None:
            return None

        rows: list[LoanRow] = []
        for loan in sorted(cp.loans_as_lender, key=lambda l: l.id):
            if loan.origination_date > period_end:
                continue
            opening = _outstanding_as_of(loan, day_before) if day_before >= loan.origination_date else 0.0
            disb = _movements_in(loan, period_start, period_end, MovementType.DISBURSEMENT)
            repay = _movements_in(loan, period_start, period_end, MovementType.PRINCIPAL_REPAYMENT)
            restr = _movements_in(loan, period_start, period_end, MovementType.PRINCIPAL_RESTRUCTURE)
            closing = _outstanding_as_of(loan, period_end)
            acc_end = compute_accrual(loan, period_end).accrued_interest
            acc_start = (
                compute_accrual(loan, day_before).accrued_interest
                if day_before >= loan.origination_date else 0.0
            )
            qtr_interest = acc_end - acc_start

            # Skip rows where nothing happened on a long-closed loan
            if (loan.status == LoanStatus.REPAID
                    and opening == 0 and disb == 0 and repay == 0 and restr == 0
                    and abs(qtr_interest) < 0.005):
                continue
            rows.append(LoanRow(
                loan_id=loan.id,
                description=(loan.description or loan.contract_reference or f"Loan #{loan.id}"),
                currency=loan.currency,
                opening=opening,
                disbursed=disb,
                repaid=repay,
                restructure=restr,
                closing=closing,
                accrued_in_qtr=qtr_interest,
                accrued_cumulative=acc_end,
            ))

        if not rows:
            return None

        return StatementData(
            counterparty_id=cp.id,
            counterparty_name=cp.name,
            year=year,
            quarter=quarter,
            period_start=period_start,
            period_end=period_end,
            generated_on=date.today(),
            loans=rows,
        )
    finally:
        session.close()


# --- PDF rendering ---------------------------------------------------------

def _money(value: float) -> str:
    return f"{value:,.2f}"


def _render_pdf(data: StatementData, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title=f"MesiCap Loan Statement {data.year} Q{data.quarter}",
    )

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("Heading1", parent=base["Heading1"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("Heading2", parent=base["Heading2"], fontSize=12, spaceAfter=4, textColor=colors.HexColor("#555555"))
    body = ParagraphStyle("Body", parent=base["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("Small", parent=base["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#777777"))
    footer = ParagraphStyle("Footer", parent=base["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#555555"))

    story = []
    story.append(Paragraph(ISSUER_HEADER, small))
    story.append(Paragraph(TITLE, h1))
    story.append(Paragraph(SUBTITLE_FMT.format(year=data.year, quarter=data.quarter,
                                               counterparty_name=data.counterparty_name), h2))
    story.append(Paragraph(
        f"Period: {data.period_start.strftime('%d.%m.%Y')} – {data.period_end.strftime('%d.%m.%Y')}",
        body))
    story.append(Spacer(1, 8))

    # Group by currency so totals make sense
    by_ccy: dict[str, list[LoanRow]] = {}
    for r in data.loans:
        by_ccy.setdefault(r.currency, []).append(r)

    for ccy, rows in by_ccy.items():
        story.append(Paragraph(f"Loans in {ccy}", h2))

        table_data = [[
            HEADER_COL_LOAN, HEADER_COL_OPENING, HEADER_COL_DISB, HEADER_COL_REPAY,
            HEADER_COL_RESTR, HEADER_COL_CLOSING, HEADER_COL_ACCRUED_Q, HEADER_COL_ACCRUED_CUM,
        ]]
        total_open = total_disb = total_repay = total_restr = total_close = total_qi = total_cum = 0.0
        for r in rows:
            short_desc = (r.description[:38] + "…") if len(r.description) > 40 else r.description
            table_data.append([
                f"#{r.loan_id} {short_desc}",
                _money(r.opening),
                _money(r.disbursed),
                _money(-r.repaid) if r.repaid else "—",
                _money(r.restructure) if r.restructure else "—",
                _money(r.closing),
                _money(r.accrued_in_qtr),
                _money(r.accrued_cumulative),
            ])
            total_open += r.opening
            total_disb += r.disbursed
            total_repay += r.repaid
            total_restr += r.restructure
            total_close += r.closing
            total_qi += r.accrued_in_qtr
            total_cum += r.accrued_cumulative

        table_data.append([
            f"Total {ccy}",
            _money(total_open), _money(total_disb),
            _money(-total_repay) if total_repay else "—",
            _money(total_restr) if total_restr else "—",
            _money(total_close), _money(total_qi), _money(total_cum),
        ])

        table = Table(table_data, repeatRows=1, hAlign="LEFT",
                      colWidths=[55*mm] + [18*mm]*7)
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eeeeee")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#222222")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
        story.append(Spacer(1, 12))

    story.append(Spacer(1, 8))
    story.append(Paragraph(LABEL_GENERATED_BY.format(date=data.generated_on.strftime("%d.%m.%Y")), footer))
    story.append(Paragraph(LABEL_CONTACT, footer))
    story.append(Spacer(1, 6))
    story.append(Paragraph(LABEL_DISCLAIMER, small))

    doc.build(story)
    return out_path


def generate_quarterly_statement(counterparty_id: int, year: int, quarter: int,
                                 storage_dir: Path | str = STATEMENTS_DIR) -> Optional[Path]:
    """Generate one lender's quarterly statement PDF. Returns the path written,
    or None if the lender had no relevant activity to report on for the quarter."""
    data = collect_statement_data(counterparty_id, year, quarter)
    if data is None:
        return None
    out_path = Path(storage_dir) / str(counterparty_id) / f"{year}-Q{quarter}.pdf"
    return _render_pdf(data, out_path)


def generate_quarter_for_all_lenders(year: int, quarter: int,
                                     storage_dir: Path | str = STATEMENTS_DIR) -> dict:
    """Run statement generation for every lender counterparty.
    Returns a summary dict with keys: written, skipped_empty."""
    session = get_session_factory()()
    try:
        cps = session.query(Counterparty).all()
        written = 0
        skipped = 0
        for cp in cps:
            # Skip MesiCap itself and any counterparty that's never been a lender
            if not cp.loans_as_lender:
                continue
            path = generate_quarterly_statement(cp.id, year, quarter, storage_dir)
            if path is None:
                skipped += 1
            else:
                written += 1
        return {"written": written, "skipped_empty": skipped}
    finally:
        session.close()


def list_statements(counterparty_id: int, storage_dir: Path | str = STATEMENTS_DIR) -> list[dict]:
    """Return statement files on disk for one counterparty, newest first.
    Each dict has keys: filename, year, quarter, size_bytes, mtime."""
    d = Path(storage_dir) / str(counterparty_id)
    if not d.exists():
        return []
    out: list[dict] = []
    for p in sorted(d.glob("*.pdf"), reverse=True):
        stem = p.stem  # e.g. "2026-Q2"
        try:
            y, q = stem.split("-Q")
            y_int, q_int = int(y), int(q)
        except (ValueError, AttributeError):
            continue
        out.append({
            "filename": p.name,
            "year": y_int,
            "quarter": q_int,
            "size_bytes": p.stat().st_size,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime),
        })
    return out
