"""
Merit Aktiva quarterly reconciliation (governance.md §4.2).

After the bookkeeper has posted the quarter to Merit, Bruno pulls Merit's
closing balances for the lender-account family and diffs them per-lender
against Bruno's own closing outstandings for the same period. Non-zero
diffs surface as a banner on `/borrower/merit-reconcile`, with traffic-light
status (≤ €1 = green, ≤ €100 = amber, > €100 = red).

Mapping: each lender Counterparty carries an optional `merit_account_id` that
ties it to one Merit account. Counterparties without a mapping render as
"not mapped" in the reconciliation table and are not silently skipped.

This module is pure-data: it reads from `merit_balances` (staging) + the live
Bruno DB and returns a report dataclass. The actual data-source (live API
vs CSV import) lives in `src/borrower/merit_api.py` / a CSV import route.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional

from src.borrower.merit_export import _quarter_bounds, _outstanding_as_of
from src.borrower.models import (
    Counterparty, Loan, LoanStatus, MeritBalance, get_session_factory,
)


GREEN_EUR = 1.0
AMBER_EUR = 100.0


@dataclass(frozen=True)
class ReconcileRow:
    cp_id: int
    cp_name: str
    merit_account_id: Optional[str]
    merit_account_name: Optional[str]
    currency: str
    bruno_closing: float
    merit_closing: Optional[float]
    diff: Optional[float]
    status: str   # 'green' | 'amber' | 'red' | 'no_merit_balance' | 'not_mapped'


@dataclass(frozen=True)
class ReconcileReport:
    year: int
    quarter: int
    period_start: date
    period_end: date
    rows: List[ReconcileRow]
    pulled_at: Optional[str]  # iso datetime of the most recent merit_balance row
    source: Optional[str]     # 'api' | 'csv_import' (most recent)

    @property
    def red_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "red")

    @property
    def amber_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "amber")

    @property
    def unmapped_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "not_mapped")

    @property
    def missing_merit_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "no_merit_balance")


def _classify(diff: Optional[float]) -> str:
    if diff is None:
        return "no_merit_balance"
    d = abs(diff)
    if d <= GREEN_EUR:
        return "green"
    if d <= AMBER_EUR:
        return "amber"
    return "red"


def reconcile_quarter(year: int, quarter: int) -> ReconcileReport:
    """Build a reconciliation report for the given quarter."""
    period_start, period_end = _quarter_bounds(year, quarter)

    session = get_session_factory()()
    try:
        # Pull the most recent merit_balance per merit_account_id within the period
        merit_rows = (
            session.query(MeritBalance)
            .filter(MeritBalance.period_start == period_start,
                    MeritBalance.period_end == period_end)
            .all()
        )
        merit_by_acct = {m.merit_account_id: m for m in merit_rows}

        # Most-recent metadata across all rows for this period (for the header)
        latest = sorted(merit_rows, key=lambda r: r.pulled_at, reverse=True)
        pulled_at = latest[0].pulled_at.isoformat() if latest else None
        source = latest[0].source if latest else None

        # Walk all counterparties that are or were lenders
        cps = session.query(Counterparty).order_by(Counterparty.name).all()
        rows: List[ReconcileRow] = []
        for cp in cps:
            loans_as_lender = cp.loans_as_lender
            if not loans_as_lender:
                continue
            # Bruno's closing outstanding for this lender at period_end
            bruno_close = 0.0
            currency = None
            for loan in loans_as_lender:
                if loan.origination_date > period_end:
                    continue
                bruno_close += _outstanding_as_of(loan, period_end)
                currency = currency or loan.currency  # first non-None currency

            # If lender had no activity in this period, skip
            if abs(bruno_close) < 0.005 and all(l.status == LoanStatus.REPAID for l in loans_as_lender):
                continue

            merit_acct_id = (cp.merit_account_id or "").strip() or None
            if merit_acct_id is None:
                rows.append(ReconcileRow(
                    cp_id=cp.id, cp_name=cp.name,
                    merit_account_id=None, merit_account_name=None,
                    currency=currency or "EUR",
                    bruno_closing=bruno_close,
                    merit_closing=None, diff=None,
                    status="not_mapped",
                ))
                continue

            m = merit_by_acct.get(merit_acct_id)
            if m is None:
                rows.append(ReconcileRow(
                    cp_id=cp.id, cp_name=cp.name,
                    merit_account_id=merit_acct_id, merit_account_name=None,
                    currency=currency or "EUR",
                    bruno_closing=bruno_close,
                    merit_closing=None, diff=None,
                    status="no_merit_balance",
                ))
                continue

            diff = bruno_close - m.closing_balance
            rows.append(ReconcileRow(
                cp_id=cp.id, cp_name=cp.name,
                merit_account_id=merit_acct_id,
                merit_account_name=m.merit_account_name,
                currency=m.currency,
                bruno_closing=bruno_close,
                merit_closing=m.closing_balance,
                diff=diff,
                status=_classify(diff),
            ))

        return ReconcileReport(
            year=year, quarter=quarter,
            period_start=period_start, period_end=period_end,
            rows=rows, pulled_at=pulled_at, source=source,
        )
    finally:
        session.close()
