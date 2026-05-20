"""
Bruno — offline backup ledger.

Per docs/governance.md §2.2: if bruno.db is corrupted or the dashboard is
offline, we still need to know who is owed what on a given date. A weekly
CSV snapshot of every active loan + accrued interest + next scheduled
payment serves as that fallback ledger. Stored under data/backups/,
filename includes ISO year + week so re-runs of the same week overwrite
rather than accumulate (idempotent on filename).

The email-out step (governance.md §2.2 also calls for emailing the CSV to
all three principals + saving to cloud storage) is Phase 2.5 and lives in
a separate handler. This module only writes the file.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional

from src.borrower.accrual import compute_accrual
from src.borrower.models import (
    Loan, LoanMovement, LoanStatus, MovementType, Payment, PaymentStatus,
    get_session_factory,
)


_BrunoSession = get_session_factory()


def _outstanding(loan: Loan) -> float:
    out = 0.0
    for m in loan.movements:
        if m.movement_type == MovementType.DISBURSEMENT:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            out -= m.amount
    return out


def _next_payment(loan: Loan) -> tuple[Optional[date], Optional[float]]:
    """Earliest scheduled (unpaid, not waived/cancelled) payment, if any."""
    pending = [
        p for p in loan.payments
        if p.status in (PaymentStatus.SCHEDULED, PaymentStatus.OVERDUE)
    ]
    if not pending:
        return None, None
    p = min(pending, key=lambda p: p.scheduled_date)
    return p.scheduled_date, p.scheduled_amount


def write_ledger(out_dir: str | Path = "data/backups", as_of: Optional[date] = None) -> Path:
    """
    Write a CSV ledger of all active loans to data/backups/ledger-YYYY-Www.csv.
    Idempotent on filename (re-running in the same ISO week overwrites).
    Returns the path written.
    """
    as_of = as_of or date.today()
    iso_year, iso_week, _ = as_of.isocalendar()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ledger-{iso_year}-W{iso_week:02d}.csv"

    session = _BrunoSession()
    try:
        loans = session.query(Loan).filter(Loan.status == LoanStatus.ACTIVE).order_by(Loan.id).all()
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "as_of",
                "loan_id",
                "lender_name",
                "currency",
                "outstanding",
                "accrued_interest",
                "total_owed",
                "rate_pct",
                "maturity_date",
                "next_payment_date",
                "next_payment_amount",
                "contract_reference",
            ])
            for loan in loans:
                outstanding = _outstanding(loan)
                acc = compute_accrual(loan, as_of)
                np_date, np_amount = _next_payment(loan)
                w.writerow([
                    as_of.isoformat(),
                    loan.id,
                    loan.lender.name,
                    loan.currency,
                    f"{outstanding:.2f}",
                    f"{acc.accrued_interest:.2f}",
                    f"{acc.total_owed:.2f}",
                    f"{loan.interest_rate_annual * 100:.4f}",
                    loan.maturity_date.isoformat() if loan.maturity_date else "",
                    np_date.isoformat() if np_date else "",
                    f"{np_amount:.2f}" if np_amount is not None else "",
                    loan.contract_reference or "",
                ])
    finally:
        session.close()
    return out_path
