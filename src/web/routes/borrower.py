"""
Borrower web route — Bruno loan portfolio management section.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func

from src.web.template_engine import templates
from src.core.logger import get_logger
from src.borrower.accrual import compute_accrual
from src.borrower.models import (
    Loan, LoanMovement, MovementType, LoanAmendment, Payment, PaymentStatus,
    LoanStatus, RepaymentStructure, LoanType, InterestRateType,
    DayCountConvention, InterestTreatment, PaymentFrequency, LoanPurpose,
    Counterparty, get_session_factory,
)

router = APIRouter()
log = get_logger(__name__)

BrunoSession = get_session_factory()


@router.get("/", response_class=HTMLResponse)
def borrower_landing(request: Request):
    return templates.TemplateResponse("borrower.html", {"request": request})


@router.get("/loans", response_class=HTMLResponse)
def borrower_loans(request: Request):
    """Loans subpage — admin view of loan portfolio."""
    session = BrunoSession()
    try:
        loans = session.query(Loan).filter(
            Loan.status == LoanStatus.ACTIVE
        ).order_by(Loan.origination_date).all()

        loan_rows = []
        totals_by_currency = defaultdict(lambda: {"outstanding": 0.0, "facility": 0.0})
        totals_by_purpose = defaultdict(lambda: defaultdict(float))

        for loan in loans:
            disbursed_cash = session.query(
                func.coalesce(func.sum(LoanMovement.amount), 0)
            ).filter_by(loan_id=loan.id, movement_type=MovementType.DISBURSEMENT).scalar() or 0.0

            restructure_adj = session.query(
                func.coalesce(func.sum(LoanMovement.amount), 0)
            ).filter_by(loan_id=loan.id, movement_type=MovementType.PRINCIPAL_RESTRUCTURE).scalar() or 0.0

            repaid = session.query(
                func.coalesce(func.sum(LoanMovement.amount), 0)
            ).filter_by(loan_id=loan.id, movement_type=MovementType.PRINCIPAL_REPAYMENT).scalar() or 0.0

            outstanding = disbursed_cash + restructure_adj - repaid
            is_revolving = loan.repayment_structure == RepaymentStructure.REVOLVING
            headroom = max(0.0, loan.principal_max - outstanding) if is_revolving else None
            utilization_pct = (outstanding / loan.principal_max * 100) if loan.principal_max else 0

            paid_payments_count = session.query(Payment).filter_by(
                loan_id=loan.id, status=PaymentStatus.PAID
            ).count()

            loan_rows.append({
                "id": loan.id,
                "lender_name": loan.lender.name,
                "purpose": loan.purpose.value,
                "loan_type": loan.loan_type.value,
                "description": loan.description,
                "principal_max": loan.principal_max,
                "disbursed_cash": disbursed_cash,
                "restructure_adj": restructure_adj,
                "repaid": repaid,
                "outstanding": outstanding,
                "headroom": headroom,
                "is_revolving": is_revolving,
                "utilization_pct": utilization_pct,
                "currency": loan.currency,
                "rate": loan.interest_rate_annual * 100,
                "interest_treatment": loan.interest_treatment.value,
                "origination_date": loan.origination_date,
                "maturity_date": loan.maturity_date,
                "is_subordinated": loan.is_subordinated,
                "collateral": loan.collateral_description,
                "paid_payments_count": paid_payments_count,
                "notes": loan.notes,
            })

            totals_by_currency[loan.currency]["outstanding"] += outstanding
            totals_by_currency[loan.currency]["facility"] += loan.principal_max
            totals_by_purpose[loan.purpose.value][loan.currency] += outstanding

        totals_by_currency = dict(totals_by_currency)
        totals_by_purpose = {k: dict(v) for k, v in totals_by_purpose.items()}

        return templates.TemplateResponse("borrower_loans.html", {
            "request": request,
            "loans": loan_rows,
            "totals_by_currency": totals_by_currency,
            "totals_by_purpose": totals_by_purpose,
        })
    finally:
        session.close()


@router.get("/loans/{loan_id}", response_class=HTMLResponse)
def borrower_loan_detail(request: Request, loan_id: int):
    """Loan detail page — full picture of one loan."""
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")

        disbursed_cash = sum(
            m.amount for m in loan.movements
            if m.movement_type == MovementType.DISBURSEMENT
        )
        restructure_adj = sum(
            m.amount for m in loan.movements
            if m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE
        )
        repaid = sum(
            m.amount for m in loan.movements
            if m.movement_type == MovementType.PRINCIPAL_REPAYMENT
        )
        outstanding = disbursed_cash + restructure_adj - repaid

        movements_sorted = sorted(loan.movements, key=lambda m: m.movement_date)
        amendments_sorted = sorted(loan.amendments, key=lambda a: a.amendment_date)
        payments_sorted = sorted(loan.payments, key=lambda p: p.scheduled_date)
        payments_paid = [p for p in payments_sorted if p.status == PaymentStatus.PAID]
        payments_pending = [p for p in payments_sorted if p.status == PaymentStatus.SCHEDULED]

        days_to_maturity = (loan.maturity_date - date.today()).days

        is_revolving = loan.repayment_structure == RepaymentStructure.REVOLVING
        headroom = max(0.0, loan.principal_max - outstanding) if is_revolving else None

        accrual = compute_accrual(loan, date.today())
        return templates.TemplateResponse("borrower_loan_detail.html", {
            "request": request,
            "loan": loan,
            "disbursed_cash": disbursed_cash,
            "restructure_adj": restructure_adj,
            "repaid": repaid,
            "outstanding": outstanding,
            "is_revolving": is_revolving,
            "headroom": headroom,
            "days_to_maturity": days_to_maturity,
            "movements": movements_sorted,
            "amendments": amendments_sorted,
            "payments": payments_sorted,
            "payments_paid_count": len(payments_paid),
            "payments_pending_count": len(payments_pending),
            "rate_pct": loan.interest_rate_annual * 100,
            "accrual": accrual,
        })
    finally:
        session.close()


@router.get("/lender-admin", response_class=HTMLResponse)
def borrower_lender_admin(request: Request):
    return templates.TemplateResponse("borrower_lender_admin.html", {"request": request})


@router.get("/loans-new", response_class=HTMLResponse)
def borrower_loan_new_form(request: Request):
    """Show the New Loan form."""
    session = BrunoSession()
    try:
        counterparties = session.query(Counterparty).order_by(Counterparty.name).all()
        return templates.TemplateResponse("borrower_loan_new.html", {
            "request": request,
            "counterparties": counterparties,
            "loan_types": [t.value for t in LoanType],
            "purposes": [p.value for p in LoanPurpose],
            "repayment_structures": [r.value for r in RepaymentStructure],
            "interest_rate_types": [t.value for t in InterestRateType],
            "day_count_conventions": [d.value for d in DayCountConvention],
            "interest_treatments": [t.value for t in InterestTreatment],
            "payment_frequencies": [f.value for f in PaymentFrequency],
            "currencies": ["EUR", "USD", "AUD", "GBP"],
            "form_data": {},
            "errors": {},
        })
    finally:
        session.close()


@router.post("/loans-new")
def borrower_loan_new_submit(
    request: Request,
    lender_id: int = Form(...),
    contract_reference: str = Form(...),
    description: str = Form(""),
    loan_type: str = Form(...),
    purpose: str = Form(...),
    repayment_structure: str = Form(...),
    principal_max: float = Form(...),
    currency: str = Form(...),
    interest_rate_pct: float = Form(...),
    interest_rate_type: str = Form("fixed"),
    day_count_convention: str = Form("act_360"),
    interest_treatment: str = Form(...),
    payment_frequency: str = Form(...),
    payment_day_of_month: int = Form(None),
    installment_amount: float = Form(None),
    contract_date: str = Form(...),
    origination_date: str = Form(...),
    maturity_date: str = Form(...),
    collateral_description: str = Form(""),
    parent_loan_description: str = Form(""),
    is_subordinated: bool = Form(False),
    early_repayment_allowed: bool = Form(True),
    early_repayment_notice_days: int = Form(30),
    notes: str = Form(""),
    initial_status: str = Form("draft"),
):
    """Process the New Loan form submission."""
    from datetime import datetime
    session = BrunoSession()
    try:
        parse = lambda s: datetime.strptime(s, "%Y-%m-%d").date()
        loan = Loan(
            lender_id=lender_id,
            borrower_id=session.query(Counterparty).filter_by(name="MesiCap Technologies OÜ").first().id,
            contract_reference=contract_reference.strip(),
            description=description.strip() or None,
            loan_type=LoanType(loan_type),
            purpose=LoanPurpose(purpose),
            repayment_structure=RepaymentStructure(repayment_structure),
            principal_max=principal_max,
            currency=currency,
            interest_rate_type=InterestRateType(interest_rate_type),
            interest_rate_annual=interest_rate_pct / 100.0,
            day_count_convention=DayCountConvention(day_count_convention),
            interest_treatment=InterestTreatment(interest_treatment),
            payment_frequency=PaymentFrequency(payment_frequency),
            payment_day_of_month=payment_day_of_month,
            installment_amount=installment_amount,
            contract_date=parse(contract_date),
            origination_date=parse(origination_date),
            maturity_date=parse(maturity_date),
            collateral_description=collateral_description.strip() or None,
            parent_loan_description=parent_loan_description.strip() or None,
            is_subordinated=is_subordinated,
            early_repayment_allowed=early_repayment_allowed,
            early_repayment_notice_days=early_repayment_notice_days,
            notes=notes.strip() or None,
            status=LoanStatus(initial_status),
        )
        session.add(loan)
        session.commit()
        session.refresh(loan)
        return RedirectResponse(url=f"/borrower/loans/{loan.id}", status_code=303)
    except Exception as e:
        session.rollback()
        log.error(f"loan_create_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to create loan: {e}")
    finally:
        session.close()


@router.get("/counterparties-new", response_class=HTMLResponse)
def borrower_counterparty_new_form(request: Request):
    """Show the New Counterparty form."""
    from src.borrower.models import CounterpartyType, CounterpartyTier
    return templates.TemplateResponse("borrower_counterparty_new.html", {
        "request": request,
        "counterparty_types": [t.value for t in CounterpartyType],
        "counterparty_tiers": [t.value for t in CounterpartyTier],
    })


@router.post("/counterparties-new")
def borrower_counterparty_new_submit(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    tier: str = Form(""),
    legal_form: str = Form(""),
    registration_number: str = Form(""),
    country: str = Form("EE"),
    address: str = Form(""),
    iban: str = Form(""),
    secondary_iban: str = Form(""),
    related_principal: str = Form(""),
    contact_email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
):
    """Process the New Counterparty form submission."""
    from src.borrower.models import CounterpartyType, CounterpartyTier
    session = BrunoSession()
    try:
        cp = Counterparty(
            name=name.strip(),
            type=CounterpartyType(type),
            tier=CounterpartyTier(tier) if tier else None,
            legal_form=legal_form.strip() or None,
            registration_number=registration_number.strip() or None,
            country=country.strip() or None,
            address=address.strip() or None,
            iban=iban.strip() or None,
            secondary_iban=secondary_iban.strip() or None,
            related_principal=related_principal.strip() or None,
            contact_email=contact_email.strip() or None,
            phone=phone.strip() or None,
            notes=notes.strip() or None,
        )
        session.add(cp)
        session.commit()
        session.refresh(cp)
        return RedirectResponse(url="/borrower/loans-new", status_code=303)
    except Exception as e:
        session.rollback()
        log.error(f"counterparty_create_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to create counterparty: {e}")
    finally:
        session.close()
