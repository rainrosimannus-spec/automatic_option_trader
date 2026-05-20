"""
Bruno loan-portfolio analytics.

Computes the time-series + databox figures shown on /borrower/loans.

Time-series functions all return list[dict] suitable for direct JSON-encoding
into a Chart.js dataset. Each point has `label` (str, e.g. "2025-11") and one
or more numeric fields.

Performance: we monthly-aggregate against the `interest_accruals` snapshots
table (one row per loan per day, ~819 rows today). Monthly sampling keeps
the result light enough to render inline.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func

from src.borrower.models import (
    Counterparty, CounterpartyTier, InterestAccrual, Loan, LoanStatus,
    MovementType, Payment, PaymentStatus, get_session_factory,
)


# ============================================================================
# Helpers
# ============================================================================

def _outstanding(loan: Loan, as_of: Optional[date] = None) -> float:
    out = 0.0
    for m in loan.movements:
        if as_of is not None and m.movement_date > as_of:
            continue
        if m.movement_type == MovementType.DISBURSEMENT:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            out -= m.amount
    return out


def _months_between(start: date, end: date) -> list[date]:
    """First-of-month dates between start and end inclusive."""
    out: list[date] = []
    y, m = start.year, start.month
    while True:
        d = date(y, m, 1)
        if d > end:
            break
        out.append(d)
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def _month_label(d: date) -> str:
    return d.strftime("%Y-%m")


# ============================================================================
# 1. Cost of capital over time
# ============================================================================

def cost_of_capital_timeseries(session) -> list[dict]:
    """Weighted-average interest rate by month, weighted by principal balance.

    Source: interest_accruals snapshots. For each first-of-month between
    Bruno's earliest loan origination and today, sum (principal × rate) and
    sum (principal); the ratio is the weighted-average rate for that month.

    Returns [{label, rate_pct, total_principal_eur}, ...].
    """
    snaps = (
        session.query(InterestAccrual)
        .join(Loan, InterestAccrual.loan_id == Loan.id)
        .order_by(InterestAccrual.accrual_date)
        .all()
    )
    if not snaps:
        return []

    # Group by month: one snapshot per loan per month (use last snapshot in the month)
    by_month: dict[tuple[int, int], dict[int, InterestAccrual]] = defaultdict(dict)
    for s in snaps:
        key = (s.accrual_date.year, s.accrual_date.month)
        by_month[key][s.loan_id] = s   # later snapshot in month overwrites earlier

    out: list[dict] = []
    today = date.today()
    if not by_month:
        return out
    start = date(*min(by_month.keys()), 1)
    months = _months_between(start, today)
    for m in months:
        key = (m.year, m.month)
        snap_set = by_month.get(key, {})
        weighted_sum = 0.0
        principal_sum = 0.0
        for s in snap_set.values():
            p = float(s.principal_balance or 0.0)
            r = float(s.interest_rate or 0.0)
            weighted_sum += p * r
            principal_sum += p
        if principal_sum > 0:
            rate = (weighted_sum / principal_sum) * 100.0
        else:
            rate = None
        out.append({
            "label": _month_label(m),
            "rate_pct": rate,
            "total_principal_eur": principal_sum,
        })
    return out


# ============================================================================
# 2. Debt outstanding over time (proxy for LTV when NLV history not available)
# ============================================================================

def debt_outstanding_timeseries(session) -> list[dict]:
    """Total outstanding principal across all active loans, by month.

    Returns [{label, total_eur, external_eur, subordinated_eur}, ...]

    Note: this is the *debt* side of LTV. Until NLV is being snapshotted
    daily by the IBKR job (gated to Rasmus's clone), we can't plot LTV
    properly — only debt outstanding. The current-LTV number is in the
    databox.
    """
    snaps = (
        session.query(InterestAccrual, Loan, Counterparty)
        .join(Loan, InterestAccrual.loan_id == Loan.id)
        .join(Counterparty, Loan.lender_id == Counterparty.id)
        .order_by(InterestAccrual.accrual_date)
        .all()
    )
    if not snaps:
        return []

    # Bucket: month → loan_id → (principal, is_external)
    by_month: dict[tuple[int, int], dict[int, tuple[float, bool]]] = defaultdict(dict)
    for s, ln, cp in snaps:
        key = (s.accrual_date.year, s.accrual_date.month)
        is_external = (cp.tier != CounterpartyTier.SHAREHOLDER)
        by_month[key][ln.id] = (float(s.principal_balance or 0.0), is_external)

    out: list[dict] = []
    if not by_month:
        return out
    start = date(*min(by_month.keys()), 1)
    months = _months_between(start, date.today())
    for m in months:
        key = (m.year, m.month)
        external = 0.0
        sub = 0.0
        for principal, is_ext in by_month.get(key, {}).values():
            if is_ext:
                external += principal
            else:
                sub += principal
        out.append({
            "label": _month_label(m),
            "total_eur": external + sub,
            "external_eur": external,
            "subordinated_eur": sub,
        })
    return out


# ============================================================================
# 3. Forward debt service — last 6 + next 18 months
# ============================================================================

def forward_debt_service(session) -> list[dict]:
    """Monthly scheduled vs actually-paid totals, for last 6 + next 18 months.

    Returns [{label, scheduled_eur, paid_eur, is_future}, ...].
    """
    today = date.today()
    horizon_start = date(today.year, today.month, 1) - timedelta(days=1)
    # back 6 months
    y, m = horizon_start.year, horizon_start.month
    for _ in range(6):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    start = date(y, m, 1)
    end_y, end_m = today.year, today.month
    for _ in range(18):
        end_m += 1
        if end_m == 13:
            end_m = 1
            end_y += 1
    end = date(end_y, end_m, 1)

    months = _months_between(start, end)

    payments = session.query(Payment).filter(
        Payment.scheduled_date >= start,
        Payment.scheduled_date < end + timedelta(days=31),
    ).all()

    scheduled_by_month: dict[tuple[int, int], float] = defaultdict(float)
    paid_by_month: dict[tuple[int, int], float] = defaultdict(float)
    for p in payments:
        if p.status in (PaymentStatus.CANCELLED, PaymentStatus.WAIVED):
            continue
        key = (p.scheduled_date.year, p.scheduled_date.month)
        scheduled_by_month[key] += float(p.scheduled_amount or 0.0)
        if p.status == PaymentStatus.PAID and p.paid_date is not None:
            paid_by_month[key] += float(p.paid_amount or p.scheduled_amount or 0.0)

    out: list[dict] = []
    today_first = date(today.year, today.month, 1)
    for m in months:
        key = (m.year, m.month)
        out.append({
            "label": _month_label(m),
            "scheduled_eur": scheduled_by_month.get(key, 0.0),
            "paid_eur": paid_by_month.get(key, 0.0),
            "is_future": m > today_first,
        })
    return out


# ============================================================================
# 4. Databox — summary stats
# ============================================================================

def dashboard_databox(session) -> dict:
    """One-shot dictionary of loan-book stats for the top-of-page databox.

    Returns a flat dict; the template just renders each field.
    """
    active_loans = session.query(Loan).filter(Loan.status == LoanStatus.ACTIVE).all()

    # Outstanding per loan + per lender
    outstanding_by_loan: dict[int, float] = {}
    outstanding_by_lender: dict[int, float] = defaultdict(float)
    cp_names: dict[int, str] = {}
    weighted_rate_num = 0.0
    weighted_rate_den = 0.0
    terms_months: list[int] = []
    external_outstanding = 0.0
    subordinated_outstanding = 0.0

    for loan in active_loans:
        o = _outstanding(loan)
        if o <= 0.005:
            continue
        outstanding_by_loan[loan.id] = o
        outstanding_by_lender[loan.lender_id] += o
        cp_names[loan.lender_id] = loan.lender.name
        weighted_rate_num += o * (loan.interest_rate_annual or 0.0)
        weighted_rate_den += o
        is_external = (loan.lender.tier != CounterpartyTier.SHAREHOLDER)
        if is_external:
            external_outstanding += o
        else:
            subordinated_outstanding += o
        # Term in months at origination
        if loan.origination_date and loan.maturity_date:
            months = (loan.maturity_date.year - loan.origination_date.year) * 12 + \
                     (loan.maturity_date.month - loan.origination_date.month)
            if months > 0:
                terms_months.append(months)

    total_outstanding = sum(outstanding_by_loan.values())
    weighted_avg_rate_pct = (weighted_rate_num / weighted_rate_den * 100.0) if weighted_rate_den > 0 else None

    avg_term_months = (sum(terms_months) / len(terms_months)) if terms_months else None

    n_active_lenders = sum(1 for cp_id, amt in outstanding_by_lender.items() if amt > 0.005)

    # Top 3 lenders by exposure
    top_lenders = sorted(outstanding_by_lender.items(), key=lambda kv: kv[1], reverse=True)[:3]
    top_3 = []
    for cp_id, amt in top_lenders:
        share = (amt / total_outstanding * 100.0) if total_outstanding > 0 else 0.0
        top_3.append({
            "cp_id": cp_id,
            "name": cp_names.get(cp_id, f"cp #{cp_id}"),
            "outstanding_eur": amt,
            "share_pct": share,
        })

    # Next scheduled payment (any loan, any lender)
    next_p = (
        session.query(Payment)
        .filter(Payment.status.in_([PaymentStatus.SCHEDULED, PaymentStatus.OVERDUE]))
        .filter(Payment.scheduled_date >= date.today())
        .order_by(Payment.scheduled_date)
        .first()
    )
    next_payment = None
    if next_p:
        next_payment = {
            "loan_id": next_p.loan_id,
            "date": next_p.scheduled_date,
            "amount_eur": float(next_p.scheduled_amount or 0.0),
            "lender_name": next_p.loan.lender.name if next_p.loan else "—",
        }

    # 12-month forward cash service
    horizon = date.today() + timedelta(days=365)
    cash_service_12m = sum(
        float(p.scheduled_amount or 0.0)
        for p in session.query(Payment)
        .filter(Payment.status.in_([PaymentStatus.SCHEDULED, PaymentStatus.OVERDUE]))
        .filter(Payment.scheduled_date >= date.today())
        .filter(Payment.scheduled_date <= horizon)
        .all()
    )

    # LTV — current snapshot only (NLV history not yet available)
    from src.borrower.headroom import get_or_init_inputs
    inputs = get_or_init_inputs(session)
    current_ltv_pct = None
    if inputs.gross_nlv_eur and inputs.gross_nlv_eur > 0:
        current_ltv_pct = (total_outstanding / inputs.gross_nlv_eur) * 100.0

    return {
        "n_active_loans": len(outstanding_by_loan),
        "n_active_lenders": n_active_lenders,
        "total_outstanding_eur": total_outstanding,
        "external_outstanding_eur": external_outstanding,
        "subordinated_outstanding_eur": subordinated_outstanding,
        "weighted_avg_rate_pct": weighted_avg_rate_pct,
        "avg_term_months": avg_term_months,
        "top_3_lenders": top_3,
        "next_payment": next_payment,
        "cash_service_12m_eur": cash_service_12m,
        "current_ltv_pct": current_ltv_pct,
        "gross_nlv_eur": float(inputs.gross_nlv_eur or 0.0),
    }
