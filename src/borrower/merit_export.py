"""
Merit Aktiva quarterly export.

Generates a CSV of per-loan-per-quarter figures that the bookkeeper imports
into Merit. Bruno never writes to Merit directly (governance.md §4.2: Bruno
does not generate journal entries automatically; the accountant owns the
chart of accounts).

Columns:
    loan_id
    lender_name
    currency
    opening_principal       — outstanding at quarter start
    disbursements_qtr       — sum of DISBURSEMENT in quarter
    repayments_qtr          — sum of PRINCIPAL_REPAYMENT in quarter
    restructures_qtr        — sum of PRINCIPAL_RESTRUCTURE in quarter (signed)
    closing_principal       — outstanding at quarter end
    interest_accrued_qtr    — accrued interest during the quarter (cumulative_end − cumulative_start)
    closing_accrued_interest — cumulative accrued interest at quarter end
    contract_reference
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Optional

from src.borrower.accrual import compute_accrual
from src.borrower.models import (
    Loan, LoanMovement, LoanStatus, MovementType, get_session_factory,
)


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    start_month = 3 * (quarter - 1) + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    last_day = 31 if end_month in (3, 5, 7, 8, 10, 12) else 30
    if end_month == 2:
        last_day = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    end = date(year, end_month, last_day)
    return start, end


def _outstanding_as_of(loan: Loan, as_of: date) -> float:
    """Sum of principal movements with movement_date <= as_of."""
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


def write_quarterly_csv(year: int, quarter: int) -> str:
    """
    Build the Merit quarterly CSV and return it as a string.
    Returns "" if no loans were active during the quarter.
    """
    start, end = _quarter_bounds(year, quarter)
    day_before_start = start - timedelta(days=1)

    session_factory = get_session_factory()
    session = session_factory()
    try:
        # Include loans originated by end-of-quarter regardless of current status,
        # so the export captures repaid loans within their final quarter.
        loans = session.query(Loan).filter(Loan.origination_date <= end).order_by(Loan.id).all()

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "loan_id", "lender_name", "currency",
            "opening_principal", "disbursements_qtr", "repayments_qtr", "restructures_qtr",
            "closing_principal",
            "interest_accrued_qtr", "closing_accrued_interest",
            "contract_reference",
        ])

        any_written = False
        for loan in loans:
            # Skip loans whose entire activity is after quarter-end
            if loan.origination_date > end:
                continue

            opening = _outstanding_as_of(loan, day_before_start) if day_before_start >= loan.origination_date else 0.0
            disb = _movements_in(loan, start, end, MovementType.DISBURSEMENT)
            repay = _movements_in(loan, start, end, MovementType.PRINCIPAL_REPAYMENT)
            restr = _movements_in(loan, start, end, MovementType.PRINCIPAL_RESTRUCTURE)
            closing = _outstanding_as_of(loan, end)

            acc_end = compute_accrual(loan, end).accrued_interest
            acc_start = (
                compute_accrual(loan, day_before_start).accrued_interest
                if day_before_start >= loan.origination_date else 0.0
            )
            qtr_interest = acc_end - acc_start

            # Skip rows where nothing happened and the loan was closed before the quarter
            if (loan.status == LoanStatus.REPAID and opening == 0 and disb == 0 and repay == 0 and restr == 0 and qtr_interest == 0):
                continue

            w.writerow([
                loan.id,
                loan.lender.name,
                loan.currency,
                f"{opening:.2f}",
                f"{disb:.2f}",
                f"{repay:.2f}",
                f"{restr:.2f}",
                f"{closing:.2f}",
                f"{qtr_interest:.2f}",
                f"{acc_end:.2f}",
                loan.contract_reference or "",
            ])
            any_written = True

        return buf.getvalue() if any_written else ""
    finally:
        session.close()
