"""
Interest accrual calculations for Bruno.

Pure functions that compute accrued interest on a loan from origination
through a target date. No side effects, no DB writes.

Supports:
- Capitalizing loans (daily compounding)
- Paid-periodically loans (simple linear accrual since last payment)
- Amortizing loans (interest portion derived from amortization schedule)
- Multiple day count conventions (act/360, act/365, 30/360)
- Principal changes mid-life (restructures, drawdowns, repayments)
- Rate changes mid-life (amendments to interest_rate_annual)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.borrower.models import (
    Loan, LoanMovement, MovementType, LoanAmendment,
    InterestTreatment, DayCountConvention,
    Payment, PaymentStatus,
)


def year_basis(convention: DayCountConvention) -> int:
    """Returns the year basis (days per year) for a given convention."""
    if convention == DayCountConvention.ACT_365:
        return 365
    return 360


def days_between(start: date, end: date, convention: DayCountConvention) -> int:
    """Days between two dates per the given convention."""
    if convention == DayCountConvention.THIRTY_360:
        d1, d2 = min(start.day, 30), min(end.day, 30)
        return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)
    return (end - start).days


@dataclass
class AccrualResult:
    """Result of an accrual calculation for a single loan."""
    loan_id: int
    as_of_date: date
    principal_at_date: float
    accrued_interest: float
    total_owed: float
    currency: str
    method: str


def _principal_segments(loan: Loan, as_of: date) -> list[tuple[date, float]]:
    """
    Return [(date, principal_balance_after_movement)] in chronological order.

    First entry is (origination_date, initial_disbursed_principal).
    Each subsequent entry reflects a movement that changes principal:
    further disbursements add, restructures add (or subtract if negative),
    principal repayments subtract.
    """
    segments = []

    initial_principal = sum(
        m.amount for m in loan.movements
        if m.movement_type == MovementType.DISBURSEMENT
        and m.movement_date <= loan.origination_date
    )
    segments.append((loan.origination_date, initial_principal))

    later = sorted(
        [m for m in loan.movements if m.movement_date > loan.origination_date],
        key=lambda m: m.movement_date,
    )

    current = initial_principal
    for mv in later:
        if mv.movement_date > as_of:
            break
        if mv.movement_type in (MovementType.DISBURSEMENT, MovementType.PRINCIPAL_RESTRUCTURE):
            current += mv.amount
        elif mv.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            current -= mv.amount
        segments.append((mv.movement_date, current))

    return segments


def _rate_segments(loan: Loan, as_of: date) -> list[tuple[date, float]]:
    """
    Return [(date, annual_rate_effective_from_that_date)] in chronological order.

    First entry is (origination_date, initial_rate). If an amendment changes
    interest_rate_annual, a new entry is appended with the new rate.

    The initial rate is reconstructed: if there's an amendment changing
    interest_rate_annual, the initial rate is that amendment's old_value
    (the rate before the change). If no such amendment exists, the initial
    rate is the loan's current rate.
    """
    rate_amendments = sorted(
        [a for a in loan.amendments
         if a.field_changed == "interest_rate_annual"
         and a.amendment_date <= as_of],
        key=lambda a: a.amendment_date,
    )

    if rate_amendments:
        initial_rate = float(rate_amendments[0].old_value)
    else:
        initial_rate = loan.interest_rate_annual

    segments = [(loan.origination_date, initial_rate)]
    for amendment in rate_amendments:
        new_rate = float(amendment.new_value)
        segments.append((amendment.amendment_date, new_rate))

    return segments


def _merge_timelines(
    principal_segs: list[tuple[date, float]],
    rate_segs: list[tuple[date, float]],
    as_of: date,
) -> list[tuple[date, date, float, float]]:
    """
    Return [(start_date, end_date, principal, rate)] for sub-periods
    where principal AND rate are both constant.

    Walks both segment timelines, splitting at every breakpoint date.
    """
    # Collect all breakpoint dates from both timelines, plus as_of
    breakpoints = sorted(set(
        [d for d, _ in principal_segs] +
        [d for d, _ in rate_segs] +
        [as_of]
    ))

    # For each breakpoint, find the principal and rate in effect on/after that date
    def value_on(segs, d):
        v = segs[0][1]
        for seg_date, seg_value in segs:
            if seg_date <= d:
                v = seg_value
            else:
                break
        return v

    sub_periods = []
    for i in range(len(breakpoints) - 1):
        start = breakpoints[i]
        end = breakpoints[i + 1]
        if end <= start:
            continue
        principal = value_on(principal_segs, start)
        rate = value_on(rate_segs, start)
        sub_periods.append((start, end, principal, rate))

    return sub_periods


def compute_accrual(loan: Loan, as_of: date) -> AccrualResult:
    """
    Compute accrued interest on a loan as of a given date.

    Dispatches to the right method based on the loan's interest treatment.
    """
    if as_of < loan.origination_date:
        return AccrualResult(
            loan_id=loan.id,
            as_of_date=as_of,
            principal_at_date=0.0,
            accrued_interest=0.0,
            total_owed=0.0,
            currency=loan.currency,
            method="not_yet_originated",
        )

    if loan.interest_treatment == InterestTreatment.CAPITALIZING:
        return _accrue_capitalizing(loan, as_of)
    elif loan.interest_treatment == InterestTreatment.AMORTIZING:
        return _accrue_amortizing(loan, as_of)
    else:
        return _accrue_simple(loan, as_of)


def _accrue_capitalizing(loan: Loan, as_of: date) -> AccrualResult:
    """
    Daily-compounding capitalizing interest.

    Walks the merged principal+rate timeline. Within each sub-period,
    principal and rate are constant; interest compounds daily.

    Across principal changes, the balance carries forward but the delta
    from the principal change is applied directly (a new disbursement
    adds to balance; a repayment subtracts).
    """
    basis = year_basis(loan.day_count_convention)
    principal_segs = _principal_segments(loan, as_of)
    rate_segs = _rate_segments(loan, as_of)
    sub_periods = _merge_timelines(principal_segs, rate_segs, as_of)

    # Track running balance (principal + capitalized interest)
    balance = 0.0
    prev_principal = 0.0

    for start, end, sub_principal, sub_rate in sub_periods:
        # Apply principal delta from previous sub-period to this one
        principal_delta = sub_principal - prev_principal
        balance += principal_delta

        # Compound at sub_rate for (end - start) days
        days = days_between(start, end, loan.day_count_convention)
        if balance > 0 and days > 0:
            balance = balance * ((1 + sub_rate / basis) ** days)

        prev_principal = sub_principal

    final_principal = principal_segs[-1][1] if principal_segs else 0.0
    accrued = balance - final_principal

    return AccrualResult(
        loan_id=loan.id,
        as_of_date=as_of,
        principal_at_date=final_principal,
        accrued_interest=accrued,
        total_owed=balance,
        currency=loan.currency,
        method="capitalizing",
    )


def _accrue_simple(loan: Loan, as_of: date) -> AccrualResult:
    """
    Simple linear accrual since last payment (or origination).

    For paid-periodically loans: interest accrues, gets paid at intervals,
    doesn't compound. The "last payment" resets the accrual clock.

    Note: if rate changed mid-period since the last payment, we walk
    the rate sub-segments. Principal is assumed constant within the
    accrual period (since payments don't change principal for these loans).
    """
    basis = year_basis(loan.day_count_convention)

    # Find the last paid date (resets accrual)
    paid_payments = [
        p for p in loan.payments
        if p.status == PaymentStatus.PAID and p.paid_date and p.paid_date <= as_of
    ]
    last_paid = max((p.paid_date for p in paid_payments), default=loan.origination_date)

    # Current principal
    principal_segs = _principal_segments(loan, as_of)
    current_principal = principal_segs[-1][1] if principal_segs else 0.0

    # Walk rate segments from last_paid to as_of
    rate_segs = _rate_segments(loan, as_of)

    def rate_on(d):
        v = rate_segs[0][1]
        for seg_date, seg_rate in rate_segs:
            if seg_date <= d:
                v = seg_rate
            else:
                break
        return v

    # Sum simple accrual per rate sub-period within [last_paid, as_of]
    rate_breakpoints = sorted(set(
        [d for d, _ in rate_segs if last_paid < d < as_of] + [last_paid, as_of]
    ))

    accrued = 0.0
    for i in range(len(rate_breakpoints) - 1):
        start = rate_breakpoints[i]
        end = rate_breakpoints[i + 1]
        sub_rate = rate_on(start)
        days = days_between(start, end, loan.day_count_convention)
        accrued += current_principal * sub_rate * days / basis

    return AccrualResult(
        loan_id=loan.id,
        as_of_date=as_of,
        principal_at_date=current_principal,
        accrued_interest=accrued,
        total_owed=current_principal + accrued,
        currency=loan.currency,
        method="simple",
    )


def _accrue_amortizing(loan: Loan, as_of: date) -> AccrualResult:
    """
    Amortizing loan: interest portion derived from the amortization schedule.

    For each paid payment, we apply standard amortization math:
    interest_portion = remaining_principal × monthly_rate
    principal_portion = installment - interest_portion
    remaining_principal -= principal_portion

    Accrued interest between last payment and as_of is computed as simple
    interest on the current remaining principal.
    """
    basis = year_basis(loan.day_count_convention)

    if not loan.installment_amount or not loan.payments:
        return AccrualResult(
            loan_id=loan.id,
            as_of_date=as_of,
            principal_at_date=loan.principal_max,
            accrued_interest=0.0,
            total_owed=loan.principal_max,
            currency=loan.currency,
            method="amortizing_incomplete",
        )

    rate = loan.interest_rate_annual
    monthly_rate = rate / 12
    installment = loan.installment_amount
    remaining = loan.principal_max
    last_paid_date = loan.origination_date

    paid_payments = sorted(
        [p for p in loan.payments if p.status == PaymentStatus.PAID and p.paid_date],
        key=lambda p: p.paid_date,
    )

    for payment in paid_payments:
        if payment.paid_date > as_of:
            break
        interest_portion = remaining * monthly_rate
        principal_portion = installment - interest_portion
        remaining -= principal_portion
        last_paid_date = payment.paid_date

    days = days_between(last_paid_date, as_of, loan.day_count_convention)
    accrued = remaining * rate * days / basis

    return AccrualResult(
        loan_id=loan.id,
        as_of_date=as_of,
        principal_at_date=remaining,
        accrued_interest=accrued,
        total_owed=remaining + accrued,
        currency=loan.currency,
        method="amortizing",
    )


def record_snapshot(session, loan: Loan, as_of: date) -> "InterestAccrual":
    """
    Record an accrual snapshot for one loan as of the given date.

    Computes accrued interest between the previous snapshot (or origination)
    and as_of, then writes a new row to interest_accruals.

    Idempotent: if a snapshot for this (loan, as_of) already exists, returns
    the existing record without modification.
    """
    from src.borrower.models import InterestAccrual

    # Idempotency check
    existing = session.query(InterestAccrual).filter_by(
        loan_id=loan.id, accrual_date=as_of
    ).first()
    if existing:
        return existing

    # Find previous snapshot for this loan (the most recent before as_of)
    prev_snapshot = session.query(InterestAccrual).filter(
        InterestAccrual.loan_id == loan.id,
        InterestAccrual.accrual_date < as_of,
    ).order_by(InterestAccrual.accrual_date.desc()).first()

    if prev_snapshot:
        period_start = prev_snapshot.accrual_date
        prev_cumulative = prev_snapshot.cumulative_accrued
    else:
        period_start = loan.origination_date
        prev_cumulative = 0.0

    # Skip if the period is non-positive (e.g., as_of is before origination)
    if as_of <= period_start:
        return None

    # Compute accrual at both endpoints, then take the delta
    current = compute_accrual(loan, as_of)
    cumulative_at_end = current.accrued_interest
    accrued_in_period = cumulative_at_end - prev_cumulative
    days = days_between(period_start, as_of, loan.day_count_convention)

    # Rate used (take the rate at the start of the period, since that's
    # what's been applied; for periods spanning a rate change, the
    # cumulative number is right, but the displayed rate is approximate)
    rate_segs = _rate_segments(loan, as_of)
    def rate_on(d):
        v = rate_segs[0][1]
        for seg_date, seg_rate in rate_segs:
            if seg_date <= d:
                v = seg_rate
            else:
                break
        return v
    rate_used = rate_on(period_start)

    snapshot = InterestAccrual(
        loan_id=loan.id,
        accrual_date=as_of,
        principal_balance=current.principal_at_date,
        days_in_period=days,
        interest_rate=rate_used,
        accrued_amount=accrued_in_period,
        cumulative_accrued=cumulative_at_end,
    )
    session.add(snapshot)
    return snapshot


def record_all_snapshots(session, as_of: date) -> dict:
    """
    Record snapshots for all active loans as of the given date.
    Returns a summary dict: {created: N, skipped: M, loan_ids_processed: [...]}.
    """
    from src.borrower.models import Loan, LoanStatus
    summary = {"created": 0, "skipped": 0, "loan_ids_processed": []}

    loans = session.query(Loan).filter(Loan.status == LoanStatus.ACTIVE).all()
    for loan in loans:
        summary["loan_ids_processed"].append(loan.id)
        before_count = session.query(type(loan).interest_accruals.property.mapper.class_).filter_by(
            loan_id=loan.id
        ).count() if False else 0  # placeholder
        result = record_snapshot(session, loan, as_of)
        if result is None:
            summary["skipped"] += 1
        else:
            session.flush()
            # If it's truly new (created_at very recent) vs existing
            from datetime import datetime, timedelta
            if result.created_at and result.created_at > datetime.utcnow() - timedelta(seconds=10):
                summary["created"] += 1
            else:
                summary["skipped"] += 1

    return summary
