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
from src.borrower.audit import snapshot, write_audit
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
def borrower_loans(request: Request, status: str = "active"):
    """Loans subpage — admin view of loan portfolio. ?status=active|all|draft|matured|repaid|defaulted|cancelled"""
    session = BrunoSession()
    try:
        status_norm = (status or "active").lower()
        q = session.query(Loan)
        if status_norm == "all":
            pass
        else:
            try:
                status_enum = LoanStatus(status_norm)
            except ValueError:
                status_enum = LoanStatus.ACTIVE
                status_norm = "active"
            q = q.filter(Loan.status == status_enum)
        loans = q.order_by(Loan.origination_date).all()

        status_counts = dict(
            session.query(Loan.status, func.count(Loan.id)).group_by(Loan.status).all()
        )
        status_counts = {s.value: c for s, c in status_counts.items()}
        status_counts["all"] = sum(status_counts.values())

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
                "lender_id": loan.lender_id,
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
            "current_status": status_norm,
            "status_counts": status_counts,
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
            "loan_statuses": [s.value for s in LoanStatus],
        })
    finally:
        session.close()


@router.post("/loans/{loan_id}/status")
def borrower_loan_change_status(
    request: Request,
    loan_id: int,
    new_status: str = Form(...),
    notes_append: str = Form(""),
):
    """Change a loan's status (ACTIVE → REPAID/MATURED/DEFAULTED/CANCELLED, etc)."""
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")
        try:
            status_enum = LoanStatus(new_status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown status: {new_status}")

        old_status = loan.status
        if status_enum == old_status:
            return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)

        before = snapshot(loan)
        loan.status = status_enum
        note = f"[STATUS] {old_status.value} → {status_enum.value}"
        if notes_append.strip():
            note += f": {notes_append.strip()}"
        loan.notes = ((loan.notes + "\n") if loan.notes else "") + note
        write_audit(session, action="status_change", entity_type="Loan", entity_id=loan.id,
                    before=before, after=snapshot(loan), request=request,
                    notes=f"{old_status.value} -> {status_enum.value}")
        session.commit()
        log.info(f"loan_status_changed loan_id={loan_id} {old_status.value}->{status_enum.value}")
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"loan_status_change_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to change status: {e}")
    finally:
        session.close()


@router.get("/lender-admin", response_class=HTMLResponse)
def borrower_lender_admin(request: Request):
    return templates.TemplateResponse("borrower_lender_admin.html", {"request": request})


@router.get("/counterparties/{cp_id}", response_class=HTMLResponse)
def borrower_counterparty_detail(request: Request, cp_id: int):
    """Counterparty detail — header, exposure rollup, loans, contact, notes."""
    session = BrunoSession()
    try:
        cp = session.query(Counterparty).filter_by(id=cp_id).first()
        if not cp:
            raise HTTPException(status_code=404, detail=f"Counterparty {cp_id} not found")

        loans_as_lender = sorted(cp.loans_as_lender, key=lambda l: l.origination_date)
        loan_rows = []
        active_exposure_by_ccy = defaultdict(float)
        facility_by_ccy = defaultdict(float)
        for loan in loans_as_lender:
            disbursed_cash = sum(
                m.amount for m in loan.movements if m.movement_type == MovementType.DISBURSEMENT
            )
            restructure_adj = sum(
                m.amount for m in loan.movements if m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE
            )
            repaid = sum(
                m.amount for m in loan.movements if m.movement_type == MovementType.PRINCIPAL_REPAYMENT
            )
            outstanding = disbursed_cash + restructure_adj - repaid
            loan_rows.append({
                "id": loan.id,
                "description": loan.description,
                "purpose": loan.purpose.value,
                "loan_type": loan.loan_type.value,
                "outstanding": outstanding,
                "currency": loan.currency,
                "principal_max": loan.principal_max,
                "rate": loan.interest_rate_annual * 100,
                "origination_date": loan.origination_date,
                "maturity_date": loan.maturity_date,
                "status": loan.status.value,
            })
            if loan.status == LoanStatus.ACTIVE:
                active_exposure_by_ccy[loan.currency] += outstanding
                facility_by_ccy[loan.currency] += loan.principal_max

        active_exposure_by_ccy = dict(active_exposure_by_ccy)
        facility_by_ccy = dict(facility_by_ccy)

        return templates.TemplateResponse("borrower_counterparty_detail.html", {
            "request": request,
            "cp": cp,
            "loans": loan_rows,
            "active_loans_count": sum(1 for l in loan_rows if l["status"] == "active"),
            "active_exposure_by_ccy": active_exposure_by_ccy,
            "facility_by_ccy": facility_by_ccy,
        })
    finally:
        session.close()


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
        session.flush()
        write_audit(session, action="create", entity_type="Loan", entity_id=loan.id,
                    after=snapshot(loan), request=request)
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
    contact_phone: str = Form(""),
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
            contact_phone=contact_phone.strip() or None,
            notes=notes.strip() or None,
        )
        session.add(cp)
        session.flush()
        write_audit(session, action="create", entity_type="Counterparty", entity_id=cp.id,
                    after=snapshot(cp), request=request)
        session.commit()
        session.refresh(cp)
        return RedirectResponse(url="/borrower/loans-new", status_code=303)
    except Exception as e:
        session.rollback()
        log.error(f"counterparty_create_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to create counterparty: {e}")
    finally:
        session.close()


@router.get("/counterparties/{cp_id}/edit", response_class=HTMLResponse)
def borrower_counterparty_edit_form(request: Request, cp_id: int):
    """Show the Edit Counterparty form prefilled with current values."""
    from src.borrower.models import CounterpartyType, CounterpartyTier
    session = BrunoSession()
    try:
        cp = session.query(Counterparty).filter_by(id=cp_id).first()
        if not cp:
            raise HTTPException(status_code=404, detail=f"Counterparty {cp_id} not found")
        return templates.TemplateResponse("borrower_counterparty_new.html", {
            "request": request,
            "cp": cp,
            "counterparty_types": [t.value for t in CounterpartyType],
            "counterparty_tiers": [t.value for t in CounterpartyTier],
        })
    finally:
        session.close()


@router.post("/counterparties/{cp_id}/edit")
def borrower_counterparty_edit_submit(
    request: Request,
    cp_id: int,
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
    contact_phone: str = Form(""),
    notes: str = Form(""),
):
    """Process Counterparty edit submission."""
    from src.borrower.models import CounterpartyType, CounterpartyTier
    session = BrunoSession()
    try:
        cp = session.query(Counterparty).filter_by(id=cp_id).first()
        if not cp:
            raise HTTPException(status_code=404, detail=f"Counterparty {cp_id} not found")
        before = snapshot(cp)
        cp.name = name.strip()
        cp.type = CounterpartyType(type)
        cp.tier = CounterpartyTier(tier) if tier else None
        cp.legal_form = legal_form.strip() or None
        cp.registration_number = registration_number.strip() or None
        cp.country = country.strip() or None
        cp.address = address.strip() or None
        cp.iban = iban.strip() or None
        cp.secondary_iban = secondary_iban.strip() or None
        cp.related_principal = related_principal.strip() or None
        cp.contact_email = contact_email.strip() or None
        cp.contact_phone = contact_phone.strip() or None
        cp.notes = notes.strip() or None
        write_audit(session, action="update", entity_type="Counterparty", entity_id=cp.id,
                    before=before, after=snapshot(cp), request=request)
        session.commit()
        return RedirectResponse(url=f"/borrower/counterparties/{cp_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"counterparty_edit_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to update counterparty: {e}")
    finally:
        session.close()


@router.get("/loans/{loan_id}/movements-new", response_class=HTMLResponse)
def borrower_movement_new_form(request: Request, loan_id: int):
    """Show the New Movement form for a specific loan."""
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")

        return templates.TemplateResponse("borrower_movement_new.html", {
            "request": request,
            "loan": loan,
            "movement_types": [t.value for t in MovementType],
        })
    finally:
        session.close()


@router.post("/loans/{loan_id}/movements-new")
def borrower_movement_new_submit(
    request: Request,
    loan_id: int,
    movement_type: str = Form(...),
    movement_date: str = Form(...),
    amount: float = Form(...),
    currency: str = Form(...),
    bank_reference: str = Form(""),
    bank_account_iban: str = Form(""),
    description: str = Form(""),
):
    """Process the New Movement form submission."""
    from datetime import datetime
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")

        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be positive")
        if currency != loan.currency:
            raise HTTPException(
                status_code=400,
                detail=f"Currency mismatch: loan is {loan.currency}, movement was {currency}",
            )

        mv = LoanMovement(
            loan_id=loan_id,
            movement_date=datetime.strptime(movement_date, "%Y-%m-%d").date(),
            movement_type=MovementType(movement_type),
            amount=amount,
            currency=currency,
            bank_reference=bank_reference.strip() or None,
            bank_account_iban=bank_account_iban.strip() or None,
            description=description.strip() or None,
        )
        session.add(mv)
        session.flush()
        write_audit(session, action="create", entity_type="LoanMovement", entity_id=mv.id,
                    after=snapshot(mv), request=request)
        session.commit()
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"movement_create_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to create movement: {e}")
    finally:
        session.close()


@router.get("/movements/{movement_id}/edit", response_class=HTMLResponse)
def borrower_movement_edit_form(request: Request, movement_id: int):
    """Show the Edit Movement form prefilled with current values."""
    session = BrunoSession()
    try:
        movement = session.query(LoanMovement).filter_by(id=movement_id).first()
        if not movement:
            raise HTTPException(status_code=404, detail=f"Movement {movement_id} not found")
        return templates.TemplateResponse("borrower_movement_new.html", {
            "request": request,
            "loan": movement.loan,
            "movement": movement,
            "movement_types": [t.value for t in MovementType],
        })
    finally:
        session.close()


@router.post("/movements/{movement_id}/edit")
def borrower_movement_edit_submit(
    request: Request,
    movement_id: int,
    movement_type: str = Form(...),
    movement_date: str = Form(...),
    amount: float = Form(...),
    currency: str = Form(...),
    bank_reference: str = Form(""),
    bank_account_iban: str = Form(""),
    description: str = Form(""),
):
    """Update an existing movement."""
    from datetime import datetime
    session = BrunoSession()
    try:
        movement = session.query(LoanMovement).filter_by(id=movement_id).first()
        if not movement:
            raise HTTPException(status_code=404, detail=f"Movement {movement_id} not found")

        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be positive")
        if currency != movement.loan.currency:
            raise HTTPException(
                status_code=400,
                detail=f"Currency mismatch: loan is {movement.loan.currency}, movement was {currency}",
            )

        before = snapshot(movement)
        movement.movement_date = datetime.strptime(movement_date, "%Y-%m-%d").date()
        movement.movement_type = MovementType(movement_type)
        movement.amount = amount
        movement.currency = currency
        movement.bank_reference = bank_reference.strip() or None
        movement.bank_account_iban = bank_account_iban.strip() or None
        movement.description = description.strip() or None

        loan_id = movement.loan_id
        write_audit(session, action="update", entity_type="LoanMovement", entity_id=movement.id,
                    before=before, after=snapshot(movement), request=request)
        session.commit()
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"movement_edit_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to edit movement: {e}")
    finally:
        session.close()


@router.post("/movements/{movement_id}/delete")
def borrower_movement_delete(request: Request, movement_id: int):
    """Delete a movement."""
    session = BrunoSession()
    try:
        movement = session.query(LoanMovement).filter_by(id=movement_id).first()
        if not movement:
            raise HTTPException(status_code=404, detail=f"Movement {movement_id} not found")
        loan_id = movement.loan_id
        before = snapshot(movement)
        session.delete(movement)
        write_audit(session, action="delete", entity_type="LoanMovement", entity_id=movement_id,
                    before=before, request=request)
        session.commit()
        log.info(f"movement_deleted movement_id={movement_id} loan_id={loan_id}")
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"movement_delete_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to delete movement: {e}")
    finally:
        session.close()


@router.get("/loans/{loan_id}/amendments-new", response_class=HTMLResponse)
def borrower_amendment_new_form(request: Request, loan_id: int):
    """Show the New Amendment form for a specific loan."""
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")
        return templates.TemplateResponse("borrower_amendment_new.html", {
            "request": request,
            "loan": loan,
        })
    finally:
        session.close()


@router.post("/loans/{loan_id}/amendments-new")
def borrower_amendment_new_submit(
    request: Request,
    loan_id: int,
    amendment_date: str = Form(...),
    field_changed: str = Form(...),
    old_value: str = Form(""),
    new_value: str = Form(...),
    description: str = Form(""),
):
    """Process the New Amendment form submission."""
    from datetime import datetime
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")

        am = LoanAmendment(
            loan_id=loan_id,
            amendment_date=datetime.strptime(amendment_date, "%Y-%m-%d").date(),
            field_changed=field_changed.strip(),
            old_value=old_value.strip() or None,
            new_value=new_value.strip() or None,
            description=description.strip() or None,
        )
        session.add(am)
        session.flush()
        write_audit(session, action="create", entity_type="LoanAmendment", entity_id=am.id,
                    after=snapshot(am), request=request)
        session.commit()
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"amendment_create_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to create amendment: {e}")
    finally:
        session.close()


@router.get("/payments/{payment_id}/edit", response_class=HTMLResponse)
def borrower_payment_edit_form(request: Request, payment_id: int):
    """Show the Payment Edit form — used for marking paid or reverting."""
    session = BrunoSession()
    try:
        payment = session.query(Payment).filter_by(id=payment_id).first()
        if not payment:
            raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")

        return templates.TemplateResponse("borrower_payment_edit.html", {
            "request": request,
            "payment": payment,
            "loan": payment.loan,
            "payment_statuses": [s.value for s in PaymentStatus],
        })
    finally:
        session.close()


@router.post("/payments/{payment_id}/edit")
def borrower_payment_edit_submit(
    request: Request,
    payment_id: int,
    action: str = Form(...),
    new_status: str = Form(""),
    paid_date: str = Form(""),
    paid_amount: float = Form(0.0),
    bank_reference: str = Form(""),
    notes_append: str = Form(""),
):
    """
    Process a Payment edit. Two actions:
    - 'mark_paid': set status (paid/overdue/waived), record paid_date/paid_amount/bank_reference
    - 'revert': clear paid fields, set status back to scheduled
    """
    from datetime import datetime
    session = BrunoSession()
    try:
        payment = session.query(Payment).filter_by(id=payment_id).first()
        if not payment:
            raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")

        loan_id = payment.loan_id
        before = snapshot(payment)

        if action == "revert":
            payment.status = PaymentStatus.SCHEDULED
            payment.paid_date = None
            payment.paid_amount = None
            payment.bank_reference = None
            if notes_append.strip():
                existing = payment.notes or ""
                payment.notes = (existing + "\n" if existing else "") + f"[REVERTED] {notes_append.strip()}"

        elif action == "mark_paid":
            status_enum = PaymentStatus(new_status) if new_status else PaymentStatus.PAID
            payment.status = status_enum

            if status_enum == PaymentStatus.WAIVED:
                # Waived payments don't need date/amount
                payment.paid_date = None
                payment.paid_amount = None
                payment.bank_reference = None
            else:
                if not paid_date:
                    raise HTTPException(status_code=400, detail="paid_date required for non-waived payments")
                payment.paid_date = datetime.strptime(paid_date, "%Y-%m-%d").date()
                payment.paid_amount = paid_amount if paid_amount > 0 else payment.scheduled_amount
                payment.bank_reference = bank_reference.strip() or None

            if notes_append.strip():
                existing = payment.notes or ""
                payment.notes = (existing + "\n" if existing else "") + notes_append.strip()

        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

        write_audit(session, action=f"payment_{action}", entity_type="Payment", entity_id=payment.id,
                    before=before, after=snapshot(payment), request=request)
        session.commit()
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"payment_edit_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to edit payment: {e}")
    finally:
        session.close()
