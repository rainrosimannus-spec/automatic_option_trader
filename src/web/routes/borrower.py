"""
Borrower web route — Bruno loan portfolio management section.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func

from src.web.template_engine import templates
from src.core.logger import get_logger
from src.borrower.accrual import compute_accrual
from src.borrower.admin_auth import (
    SESSION_COOKIE as ADMIN_SESSION_COOKIE,
    clear_session as admin_clear_session,
    consume_magic_link as admin_consume_magic_link,
    current_principal,
    request_magic_link as admin_request_magic_link,
    set_session_cookie as admin_set_session_cookie,
)
from src.borrower.audit import snapshot, write_audit
from src.borrower.models import (
    AuditLog, Loan, LoanApproval, LoanMovement, MeritBalance, MovementType,
    LoanAmendment, Payment, PaymentStatus, LoanStatus, PrincipalUser,
    RepaymentStructure, LoanType, InterestRateType, DayCountConvention,
    InterestTreatment, PaymentFrequency, LoanPurpose, Counterparty,
    DocumentType, LoanDocument, PortalUser, get_session_factory,
)

router = APIRouter()
log = get_logger(__name__)

BrunoSession = get_session_factory()


# =============================================================================
# Admin auth — login / magic / logout. These routes are exempt from the
# auth middleware that gates the rest of /borrower/*.
# =============================================================================

@router.get("/login", response_class=HTMLResponse)
def borrower_login_form(request: Request):
    if current_principal(request) is not None:
        return RedirectResponse(url="/borrower/", status_code=303)
    return templates.TemplateResponse("borrower_login.html", {
        "request": request,
        "message": None,
        "email_value": "",
    })


@router.post("/login", response_class=HTMLResponse)
def borrower_login_submit(request: Request, email: str = Form(...)):
    result = admin_request_magic_link(email, request=request)
    message = (
        "If that email is registered as a MesiCap principal, a magic link has been sent. "
        "The link is valid for 15 minutes."
    )
    if result.get("status") == "rate_limited":
        message = "Too many requests in a short window — please wait a few minutes before trying again."
    elif result.get("status") == "locked":
        message = "This account is currently locked."
    return templates.TemplateResponse("borrower_login.html", {
        "request": request,
        "message": message,
        "email_value": email,
        "dev_magic_url": result.get("magic_url"),
    })


@router.get("/magic/{token}")
def borrower_magic_consume(request: Request, token: str):
    session_token = admin_consume_magic_link(token, request=request)
    if session_token is None:
        return templates.TemplateResponse("borrower_login.html", {
            "request": request,
            "message": "That link is invalid or has expired. Request a new one.",
            "email_value": "",
        }, status_code=400)
    response = RedirectResponse(url="/borrower/", status_code=303)
    admin_set_session_cookie(response, session_token)
    return response


@router.get("/logout")
def borrower_logout(request: Request):
    response = RedirectResponse(url="/borrower/login", status_code=303)
    admin_clear_session(request, response)
    return response


# =============================================================================
# Admin routes (all gated by the auth middleware in src/web/app.py)
# =============================================================================

@router.get("/", response_class=HTMLResponse)
def borrower_landing(request: Request):
    from src.borrower.deadman import compute_state, executor_contact
    from src.borrower.quorum import pending_approval_loans
    from src.borrower.models import ContactUpdateRequest, Counterparty
    principal = current_principal(request)
    pending = pending_approval_loans(principal.id) if principal else []
    session = BrunoSession()
    try:
        # Open contact-update requests from lenders
        open_requests_rows = (
            session.query(ContactUpdateRequest)
            .filter(ContactUpdateRequest.status == "new")
            .order_by(ContactUpdateRequest.created_at.desc())
            .limit(20).all()
        )
        cp_by_id = {c.id: c.name for c in session.query(Counterparty).all()}
        open_requests = [{
            "id": r.id,
            "cp_name": cp_by_id.get(r.counterparty_id, f"cp #{r.counterparty_id}"),
            "subject": r.subject,
            "created_at": r.created_at,
        } for r in open_requests_rows]
        return templates.TemplateResponse("borrower.html", {
            "request": request,
            "current_principal": principal,
            "deadman": compute_state(),
            "deadman_executor": executor_contact(),
            "pending_approvals": pending,
            "open_contact_requests": open_requests,
        })
    finally:
        session.close()


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

        # Tied-document rule warning: how many ACTIVE loans are missing an
        # agreement document? (docs/governance.md §1 — pre-rule legacy loans
        # are grandfathered but should be backfilled.)
        active_loans = session.query(Loan).filter(Loan.status == LoanStatus.ACTIVE).all()
        missing_agreement_ids = []
        for ln in active_loans:
            has_agreement = session.query(LoanDocument).filter_by(
                loan_id=ln.id, document_type=DocumentType.AGREEMENT,
            ).first() is not None
            if not has_agreement:
                missing_agreement_ids.append(ln.id)

        return templates.TemplateResponse("borrower_loans.html", {
            "request": request,
            "loans": loan_rows,
            "totals_by_currency": totals_by_currency,
            "totals_by_purpose": totals_by_purpose,
            "current_status": status_norm,
            "status_counts": status_counts,
            "missing_agreement_ids": missing_agreement_ids,
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
        documents_sorted = sorted(
            session.query(LoanDocument).filter_by(loan_id=loan.id).all(),
            key=lambda d: d.uploaded_at,
            reverse=True,
        )
        has_agreement = any(d.document_type == DocumentType.AGREEMENT for d in documents_sorted)
        # Access quorum state for this loan (governance.md §3.3)
        from src.borrower.quorum import quorum_state
        qstate = quorum_state(loan, session=session)
        principal_now = current_principal(request)
        principal_has_approved = (
            principal_now is not None
            and any(a["principal_id"] == principal_now.id for a in qstate.approvers)
        )
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
            "documents": documents_sorted,
            "document_types": [t.value for t in DocumentType],
            "has_agreement": has_agreement,
            "qstate": qstate,
            "principal_has_approved": principal_has_approved,
            "current_principal": principal_now,
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

        # Tied-document rule (docs/governance.md §1): a loan can only
        # transition DRAFT → ACTIVE once an agreement document is on file.
        if old_status == LoanStatus.DRAFT and status_enum == LoanStatus.ACTIVE:
            has_agreement = session.query(LoanDocument).filter_by(
                loan_id=loan.id,
                document_type=DocumentType.AGREEMENT,
            ).first() is not None
            if not has_agreement:
                raise HTTPException(
                    status_code=400,
                    detail=("Cannot activate this loan: no signed agreement on file. "
                            "Upload the signed PDF via the Documents panel on the loan detail page first."),
                )

            # Access quorum (governance.md §3.3): loans ≥ threshold need 2-of-N
            # principal approvals before activation.
            from src.borrower.quorum import quorum_state, QUORUM_THRESHOLD_EUR
            qs = quorum_state(loan, session=session)
            if qs.required and not qs.can_activate:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Quorum: this loan's face value (€{loan.principal_max:,.0f}) is at or above "
                        f"€{QUORUM_THRESHOLD_EUR:,.0f}, so {qs.needed} principal approvals are required. "
                        f"Currently have {qs.have}; need {qs.remaining} more before DRAFT → ACTIVE."
                    ),
                )

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


@router.get("/bank-transactions", response_class=HTMLResponse)
def borrower_bank_transactions(request: Request, status: str = "unmatched"):
    """List bank transactions in staging with status filter."""
    from src.borrower.models import BankTransaction
    session = BrunoSession()
    try:
        status_norm = (status or "unmatched").lower()
        q = session.query(BankTransaction)
        if status_norm != "all":
            q = q.filter(BankTransaction.status == status_norm)
        rows = q.order_by(BankTransaction.value_date.desc(), BankTransaction.id.desc()).limit(500).all()
        counts = dict(
            session.query(BankTransaction.status, func.count(BankTransaction.id))
            .group_by(BankTransaction.status).all()
        )
        counts["all"] = sum(counts.values())
        # Pre-fetch movements for the manual-match dropdown — recent unlinked-to-bt movements
        movements = session.query(LoanMovement).order_by(LoanMovement.movement_date.desc()).limit(50).all()
        return templates.TemplateResponse("borrower_bank_transactions.html", {
            "request": request,
            "rows": rows,
            "current_status": status_norm,
            "counts": counts,
            "movements": movements,
        })
    finally:
        session.close()


@router.post("/bank-transactions/upload")
def borrower_bank_transactions_upload(request: Request, file_path: str = Form(...)):
    """
    Ingest a CAMT.053 statement file from a local server-side path.

    Why path-on-server rather than browser upload: this admin tool runs in a
    trusted environment (Rain's dev box or Rasmus's clone) where the principal
    drops the file into a known folder. Browser upload could come later if
    needed; path-based keeps the dependency surface tiny.
    """
    from src.borrower.lhv_ingest import ingest_camt053_file
    session = BrunoSession()
    try:
        result = ingest_camt053_file(session, file_path)
        log.info(f"camt053_ingested file={file_path} result={result}")
        return RedirectResponse(
            url=f"/borrower/bank-transactions?status=unmatched",
            status_code=303,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    except Exception as e:
        log.error(f"camt053_ingest_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to ingest: {e}")
    finally:
        session.close()


@router.post("/bank-transactions/{bt_id}/match")
def borrower_bank_transactions_match(request: Request, bt_id: int, movement_id: int = Form(...)):
    """Manually link a bank transaction to a LoanMovement."""
    from src.borrower.models import BankTransaction
    session = BrunoSession()
    try:
        bt = session.query(BankTransaction).filter_by(id=bt_id).first()
        if not bt:
            raise HTTPException(status_code=404, detail="Bank transaction not found")
        mv = session.query(LoanMovement).filter_by(id=movement_id).first()
        if not mv:
            raise HTTPException(status_code=404, detail="Movement not found")
        before = snapshot(bt)
        bt.matched_movement_id = mv.id
        bt.status = "matched"
        write_audit(session, action="match", entity_type="BankTransaction", entity_id=bt.id,
                    before=before, after=snapshot(bt), request=request,
                    notes=f"linked to movement {mv.id}")
        session.commit()
        return RedirectResponse(url="/borrower/bank-transactions?status=unmatched", status_code=303)
    finally:
        session.close()


@router.post("/bank-transactions/{bt_id}/ignore")
def borrower_bank_transactions_ignore(request: Request, bt_id: int):
    """Mark a bank transaction as ignored (not relevant to any loan)."""
    from src.borrower.models import BankTransaction
    session = BrunoSession()
    try:
        bt = session.query(BankTransaction).filter_by(id=bt_id).first()
        if not bt:
            raise HTTPException(status_code=404, detail="Bank transaction not found")
        before = snapshot(bt)
        bt.status = "ignored"
        write_audit(session, action="ignore", entity_type="BankTransaction", entity_id=bt.id,
                    before=before, after=snapshot(bt), request=request)
        session.commit()
        return RedirectResponse(url="/borrower/bank-transactions?status=unmatched", status_code=303)
    finally:
        session.close()


@router.get("/headroom", response_class=HTMLResponse)
def borrower_headroom(request: Request):
    """Headroom Calculator — current ratios + edit form for inputs +
    'evaluate hypothetical loan' form."""
    from src.borrower.headroom import compute_headroom, get_or_init_inputs
    session = BrunoSession()
    try:
        inputs = get_or_init_inputs(session)
        report = compute_headroom(
            gross_nlv_eur=inputs.gross_nlv_eur,
            cash_eur=inputs.cash_eur,
            expected_annual_return_eur=inputs.expected_annual_return_eur,
            inputs_source=inputs.source,
            inputs_as_of=inputs.as_of.isoformat() if inputs.as_of else None,
        )
        return templates.TemplateResponse("borrower_headroom.html", {
            "request": request,
            "inputs": inputs,
            "report": report,
            "evaluation": None,
        })
    finally:
        session.close()


@router.post("/headroom/inputs")
def borrower_headroom_inputs(
    request: Request,
    gross_nlv_eur: float = Form(...),
    cash_eur: float = Form(...),
    expected_annual_return_eur: float = Form(...),
    notes: str = Form(""),
):
    """Update the manual HeadroomInputs row."""
    from datetime import datetime
    from src.borrower.headroom import get_or_init_inputs
    session = BrunoSession()
    try:
        inputs = get_or_init_inputs(session)
        before = snapshot(inputs)
        inputs.gross_nlv_eur = float(gross_nlv_eur)
        inputs.cash_eur = float(cash_eur)
        inputs.expected_annual_return_eur = float(expected_annual_return_eur)
        inputs.notes = notes.strip() or None
        inputs.source = "manual"
        inputs.as_of = datetime.utcnow()
        write_audit(session, action="update", entity_type="HeadroomInputs",
                    entity_id=inputs.id, before=before, after=snapshot(inputs),
                    request=request)
        session.commit()
        return RedirectResponse(url="/borrower/headroom", status_code=303)
    finally:
        session.close()


@router.post("/headroom/evaluate", response_class=HTMLResponse)
def borrower_headroom_evaluate(
    request: Request,
    new_principal_eur: float = Form(...),
    new_is_external: bool = Form(True),
    new_annual_cash_service_eur: float = Form(0.0),
):
    """Evaluate a hypothetical new loan against current inputs."""
    from src.borrower.headroom import compute_headroom, get_or_init_inputs
    session = BrunoSession()
    try:
        inputs = get_or_init_inputs(session)
        current = compute_headroom(
            gross_nlv_eur=inputs.gross_nlv_eur,
            cash_eur=inputs.cash_eur,
            expected_annual_return_eur=inputs.expected_annual_return_eur,
            inputs_source=inputs.source,
            inputs_as_of=inputs.as_of.isoformat() if inputs.as_of else None,
        )
        hypothetical = compute_headroom(
            gross_nlv_eur=inputs.gross_nlv_eur,
            cash_eur=inputs.cash_eur,
            expected_annual_return_eur=inputs.expected_annual_return_eur,
            new_loan_principal_eur=float(new_principal_eur),
            new_loan_is_external=bool(new_is_external),
            new_loan_annual_cash_service_eur=float(new_annual_cash_service_eur),
            inputs_source=inputs.source,
            inputs_as_of=inputs.as_of.isoformat() if inputs.as_of else None,
        )
        return templates.TemplateResponse("borrower_headroom.html", {
            "request": request,
            "inputs": inputs,
            "report": current,
            "evaluation": {
                "principal": new_principal_eur,
                "is_external": new_is_external,
                "annual_cash_service": new_annual_cash_service_eur,
                "hypothetical": hypothetical,
            },
        })
    finally:
        session.close()


@router.get("/exports", response_class=HTMLResponse)
def borrower_exports(request: Request):
    """Landing page for downloadable accounting exports."""
    from datetime import date as _date
    today = _date.today()
    # Surface the most recent 4 completed quarters
    items = []
    y, q = today.year, ((today.month - 1) // 3 + 1)
    # Go back to the most recently *completed* quarter
    q -= 1
    if q == 0:
        q = 4; y -= 1
    for _ in range(4):
        items.append({"year": y, "quarter": q,
                      "url": f"/borrower/exports/merit-{y}-Q{q}.csv",
                      "label": f"{y} Q{q}"})
        q -= 1
        if q == 0:
            q = 4; y -= 1
    return templates.TemplateResponse("borrower_exports.html", {
        "request": request,
        "items": items,
    })


@router.post("/exports/statements/{year}/{quarter}")
def borrower_generate_statements(request: Request, year: int, quarter: int):
    """Manually trigger statement PDF generation for a quarter."""
    from src.borrower.statements import generate_quarter_for_all_lenders
    try:
        result = generate_quarter_for_all_lenders(year, quarter)
        log.info(f"statements_generated_manual year={year} quarter={quarter} result={result}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"statements_generate_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed: {e}")
    return RedirectResponse(url="/borrower/exports", status_code=303)


@router.get("/contact-requests", response_class=HTMLResponse)
def borrower_contact_requests(request: Request, status: str = "new"):
    """List contact-update requests filtered by status (default 'new')."""
    from src.borrower.models import ContactUpdateRequest
    session = BrunoSession()
    try:
        q = session.query(ContactUpdateRequest)
        if status and status != "all":
            q = q.filter(ContactUpdateRequest.status == status)
        rows = q.order_by(ContactUpdateRequest.created_at.desc()).limit(200).all()
        cp_by_id = {c.id: c.name for c in session.query(Counterparty).all()}
        counts = dict(
            session.query(ContactUpdateRequest.status, func.count(ContactUpdateRequest.id))
            .group_by(ContactUpdateRequest.status).all()
        )
        counts["all"] = sum(counts.values())
        return templates.TemplateResponse("borrower_contact_requests.html", {
            "request": request,
            "rows": rows,
            "cp_by_id": cp_by_id,
            "current_status": status,
            "counts": counts,
        })
    finally:
        session.close()


@router.get("/contact-requests/{req_id}", response_class=HTMLResponse)
def borrower_contact_request_detail(request: Request, req_id: int):
    from src.borrower.models import ContactUpdateRequest, PortalUser
    session = BrunoSession()
    try:
        r = session.query(ContactUpdateRequest).filter_by(id=req_id).first()
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")
        cp = session.query(Counterparty).filter_by(id=r.counterparty_id).first()
        portal_user = session.query(PortalUser).filter_by(id=r.portal_user_id).first()
        return templates.TemplateResponse("borrower_contact_request_detail.html", {
            "request": request,
            "r": r,
            "cp": cp,
            "portal_user": portal_user,
        })
    finally:
        session.close()


@router.post("/contact-requests/{req_id}/resolve")
def borrower_contact_request_resolve(
    request: Request,
    req_id: int,
    new_status: str = Form(...),
    admin_notes: str = Form(""),
):
    """Mark a contact-update request as acknowledged / applied / rejected."""
    from src.borrower.models import ContactUpdateRequest
    if new_status not in ("new", "acknowledged", "applied", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status")
    session = BrunoSession()
    try:
        r = session.query(ContactUpdateRequest).filter_by(id=req_id).first()
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")
        before = snapshot(r)
        r.status = new_status
        if admin_notes.strip():
            existing = r.admin_notes or ""
            r.admin_notes = (existing + "\n" if existing else "") + admin_notes.strip()
        write_audit(session, action="update", entity_type="ContactUpdateRequest",
                    entity_id=r.id, before=before, after=snapshot(r), request=request,
                    notes=f"status: {before.get('status')} -> {new_status}")
        session.commit()
        return RedirectResponse(url=f"/borrower/contact-requests/{req_id}", status_code=303)
    finally:
        session.close()


@router.get("/test-mail")
def borrower_test_mail(request: Request, to: str = ""):
    """Send a test email to `to` to verify SMTP credentials.
    Returns a JSON status. Returns 400 if `to` is missing or invalid."""
    from src.borrower.mail import is_configured, send_email
    from fastapi.responses import JSONResponse
    if not to or "@" not in to:
        raise HTTPException(status_code=400, detail="Pass ?to=<email> with a valid address")
    if not is_configured():
        return JSONResponse({
            "smtp_configured": False,
            "sent": False,
            "reason": "SMTP_HOST env var not set; configure SMTP creds in .env",
        })
    result = send_email(to, "MesiCap SMTP test", "If you got this, Bruno's SMTP wiring works.\n— Bruno")
    return JSONResponse({"smtp_configured": True, **result})


@router.get("/merit-reconcile", response_class=HTMLResponse)
def borrower_merit_reconcile(request: Request, year: int = 0, quarter: int = 0):
    """Quarterly diff of Bruno's closing outstandings against Merit Aktiva's
    closing balances per lender (governance.md §4.2)."""
    from src.borrower.merit_reconcile import reconcile_quarter
    from datetime import date as _date
    # Default to the just-ended quarter if not given
    if not year or not quarter:
        today = _date.today()
        cq = ((today.month - 1) // 3) + 1
        if cq == 1:
            year, quarter = today.year - 1, 4
        else:
            year, quarter = today.year, cq - 1
    try:
        report = reconcile_quarter(year, quarter)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return templates.TemplateResponse("borrower_merit_reconcile.html", {
        "request": request,
        "report": report,
        "year": year, "quarter": quarter,
    })


@router.post("/merit-reconcile/import")
async def borrower_merit_import(
    request: Request,
    year: int = Form(...),
    quarter: int = Form(...),
    file: UploadFile = File(...),
):
    """Import a CSV exported from Merit Aktiva into the merit_balances staging
    table for a given quarter. CSV columns expected:
        merit_account_id, merit_account_name, currency, closing_balance
    Header row required. Idempotent on (period, account_id, source='csv_import')."""
    import csv as _csv
    import io
    from datetime import datetime
    from src.borrower.merit_export import _quarter_bounds
    try:
        period_start, period_end = _quarter_bounds(year, quarter)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(raw))
    required = {"merit_account_id", "currency", "closing_balance"}
    missing = required - set([h.strip() for h in reader.fieldnames or []])
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns: {sorted(missing)}. Header must include {sorted(required)}.",
        )

    session = BrunoSession()
    written, updated, skipped = 0, 0, 0
    try:
        for row in reader:
            acct = (row.get("merit_account_id") or "").strip()
            if not acct:
                skipped += 1
                continue
            try:
                closing = float((row.get("closing_balance") or "0").replace(",", "."))
            except ValueError:
                skipped += 1
                continue
            existing = (
                session.query(MeritBalance)
                .filter_by(period_start=period_start, period_end=period_end,
                           merit_account_id=acct, source="csv_import")
                .first()
            )
            name = (row.get("merit_account_name") or "").strip() or None
            currency = (row.get("currency") or "EUR").strip().upper()
            if existing is not None:
                existing.merit_account_name = name
                existing.currency = currency
                existing.closing_balance = closing
                existing.pulled_at = datetime.utcnow()
                updated += 1
            else:
                session.add(MeritBalance(
                    period_start=period_start, period_end=period_end,
                    merit_account_id=acct, merit_account_name=name,
                    currency=currency, closing_balance=closing,
                    source="csv_import",
                ))
                written += 1
        session.commit()
        log.info(f"merit_import year={year} quarter={quarter} written={written} updated={updated} skipped={skipped}")
        return RedirectResponse(
            url=f"/borrower/merit-reconcile?year={year}&quarter={quarter}", status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"merit_import_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed: {e}")
    finally:
        session.close()


@router.get("/audit", response_class=HTMLResponse)
def borrower_audit(
    request: Request,
    entity_type: str = "",
    actor: str = "",
    action: str = "",
    limit: int = 100,
):
    """Recent audit_log rows with filters. Show before/after JSON inline so the
    diff is visible without leaving the page."""
    session = BrunoSession()
    try:
        q = session.query(AuditLog)
        if entity_type:
            q = q.filter(AuditLog.entity_type == entity_type)
        if actor:
            q = q.filter(AuditLog.actor == actor)
        if action:
            q = q.filter(AuditLog.action == action)
        limit = max(10, min(int(limit or 100), 500))
        rows = q.order_by(AuditLog.timestamp.desc()).limit(limit).all()

        # Distinct values to populate the filter dropdowns
        distinct_entity_types = [r[0] for r in session.query(AuditLog.entity_type).distinct().all() if r[0]]
        distinct_actors = [r[0] for r in session.query(AuditLog.actor).distinct().all() if r[0]]
        distinct_actions = [r[0] for r in session.query(AuditLog.action).distinct().all() if r[0]]

        return templates.TemplateResponse("borrower_audit.html", {
            "request": request,
            "rows": rows,
            "entity_type": entity_type,
            "actor": actor,
            "action": action,
            "limit": limit,
            "distinct_entity_types": sorted(distinct_entity_types),
            "distinct_actors": sorted(distinct_actors),
            "distinct_actions": sorted(distinct_actions),
        })
    finally:
        session.close()


@router.get("/statements-archive", response_class=HTMLResponse)
def borrower_statements_archive(request: Request):
    """List all generated lender statement PDFs grouped per counterparty.
    Walks data/statements/ on the filesystem; cross-references counterparty
    names from Bruno."""
    from src.borrower.statements import STATEMENTS_DIR
    from pathlib import Path
    session = BrunoSession()
    try:
        cp_by_id = {cp.id: cp.name for cp in session.query(Counterparty).all()}
        groups = []
        for cp_dir in sorted(STATEMENTS_DIR.glob("*")) if STATEMENTS_DIR.exists() else []:
            if not cp_dir.is_dir():
                continue
            try:
                cp_id = int(cp_dir.name)
            except ValueError:
                continue
            pdfs = []
            for pdf in sorted(cp_dir.glob("*.pdf"), reverse=True):
                stem = pdf.stem  # YYYY-Qn
                try:
                    y, q = stem.split("-Q")
                    year, quarter = int(y), int(q)
                except (ValueError, AttributeError):
                    continue
                stat = pdf.stat()
                from datetime import datetime
                pdfs.append({
                    "filename": pdf.name,
                    "year": year, "quarter": quarter,
                    "size_kb": stat.st_size // 1024,
                    "mtime": datetime.fromtimestamp(stat.st_mtime),
                    "download_url": f"/borrower/statements-archive/{cp_id}/{pdf.name}",
                })
            if pdfs:
                groups.append({
                    "cp_id": cp_id,
                    "cp_name": cp_by_id.get(cp_id, f"Counterparty #{cp_id}"),
                    "pdfs": pdfs,
                })
        return templates.TemplateResponse("borrower_statements_archive.html", {
            "request": request,
            "groups": groups,
            "total": sum(len(g["pdfs"]) for g in groups),
        })
    finally:
        session.close()


@router.get("/statements-archive/{cp_id}/{filename}")
def borrower_statements_archive_download(request: Request, cp_id: int, filename: str):
    """Admin-side download of an issued statement PDF. Strict filename pattern
    against path traversal."""
    import re
    from src.borrower.statements import STATEMENTS_DIR
    if not re.fullmatch(r"\d{4}-Q[1-4]\.pdf", filename):
        raise HTTPException(status_code=404, detail="Not found")
    path = STATEMENTS_DIR / str(cp_id) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path), media_type="application/pdf", filename=filename)


@router.get("/exports/merit-{year}-Q{quarter}.csv")
def borrower_merit_quarterly(request: Request, year: int, quarter: int):
    """Download a Merit-formatted CSV for the given quarter (governance.md §4.2)."""
    from fastapi.responses import Response
    from src.borrower.merit_export import write_quarterly_csv
    try:
        csv_body = write_quarterly_csv(year, quarter)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not csv_body:
        # Still return a header-only CSV rather than 404, so the bookkeeper
        # gets a deterministic file for any quarter and can confirm "no activity"
        csv_body = "loan_id,lender_name,currency,opening_principal,disbursements_qtr,repayments_qtr,restructures_qtr,closing_principal,interest_accrued_qtr,closing_accrued_interest,contract_reference\n"
    filename = f"merit-{year}-Q{quarter}.csv"
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/bank-accounts", response_class=HTMLResponse)
def borrower_bank_accounts(request: Request):
    """Show registered LHV bank accounts + tally how many movements reference each."""
    from src.borrower.lhv_accounts import ACCOUNTS
    session = BrunoSession()
    try:
        rows = []
        for a in ACCOUNTS:
            mv_count = session.query(LoanMovement).filter(
                LoanMovement.bank_account_iban == a.iban
            ).count()
            rows.append({"acct": a, "movement_count": mv_count})
        # Any movements with IBANs that aren't in the registry
        all_used = session.query(LoanMovement.bank_account_iban).filter(
            LoanMovement.bank_account_iban.isnot(None)
        ).distinct().all()
        known = {a.iban for a in ACCOUNTS}
        orphans = [iban for (iban,) in all_used if iban and iban not in known]
        return templates.TemplateResponse("borrower_bank_accounts.html", {
            "request": request,
            "rows": rows,
            "orphan_ibans": orphans,
        })
    finally:
        session.close()


@router.get("/lender-admin", response_class=HTMLResponse)
def borrower_lender_admin(request: Request):
    """Admin view of all lender counterparties with KYC + exposure + count guard."""
    from src.borrower.models import CounterpartyType
    session = BrunoSession()
    try:
        # Lenders = counterparties that appear on at least one loan as lender,
        # excluding MesiCap itself (which is always borrower).
        all_cps = session.query(Counterparty).filter(
            Counterparty.type != CounterpartyType.INTERNAL
        ).order_by(Counterparty.name).all()

        rows = []
        active_lender_count = 0
        for cp in all_cps:
            loans = list(cp.loans_as_lender)
            if not loans:
                continue
            active_loans = [l for l in loans if l.status == LoanStatus.ACTIVE]
            exposure_by_ccy = defaultdict(float)
            for loan in active_loans:
                outstanding = sum(
                    m.amount if m.movement_type == MovementType.DISBURSEMENT
                    else m.amount if m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE
                    else -m.amount if m.movement_type == MovementType.PRINCIPAL_REPAYMENT
                    else 0
                    for m in loan.movements
                )
                exposure_by_ccy[loan.currency] += outstanding
            if active_loans:
                active_lender_count += 1
            rows.append({
                "id": cp.id,
                "name": cp.name,
                "tier": cp.tier.value if cp.tier else None,
                "kyc_status": cp.kyc_status or "not_required",
                "active_loans": len(active_loans),
                "total_loans": len(loans),
                "exposure_by_ccy": dict(exposure_by_ccy),
                "contact_email": cp.contact_email,
                "contact_phone": cp.contact_phone,
            })

        return templates.TemplateResponse("borrower_lender_admin.html", {
            "request": request,
            "rows": rows,
            "active_lender_count": active_lender_count,
            "limit_amber": 18,
            "limit_red": 20,
        })
    finally:
        session.close()


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

        portal_users = session.query(PortalUser).filter_by(counterparty_id=cp.id).order_by(PortalUser.created_at).all()

        return templates.TemplateResponse("borrower_counterparty_detail.html", {
            "request": request,
            "cp": cp,
            "loans": loan_rows,
            "active_loans_count": sum(1 for l in loan_rows if l["status"] == "active"),
            "active_exposure_by_ccy": active_exposure_by_ccy,
            "facility_by_ccy": facility_by_ccy,
            "portal_users": portal_users,
        })
    finally:
        session.close()


@router.post("/counterparties/{cp_id}/portal-users")
def borrower_portal_user_add(
    request: Request,
    cp_id: int,
    email: str = Form(...),
):
    """Create a portal_users row for a lender. Pilot seeding affordance."""
    from datetime import datetime
    session = BrunoSession()
    try:
        cp = session.query(Counterparty).filter_by(id=cp_id).first()
        if not cp:
            raise HTTPException(status_code=404, detail=f"Counterparty {cp_id} not found")
        email_norm = email.strip().lower()
        if not email_norm or "@" not in email_norm:
            raise HTTPException(status_code=400, detail="Valid email required")

        # Same email can map to multiple counterparties (one human can own
        # multiple lender entities) — but only one row per (email, counterparty).
        existing = session.query(PortalUser).filter_by(
            email=email_norm, counterparty_id=cp_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Email already registered for this counterparty",
            )

        pu = PortalUser(
            counterparty_id=cp_id,
            email=email_norm,
            invited_by="rain",  # hardcoded until dashboard auth ships
            invitation_date=datetime.utcnow(),
        )
        session.add(pu)
        session.flush()
        write_audit(session, action="create", entity_type="PortalUser", entity_id=pu.id,
                    after=snapshot(pu), request=request)
        session.commit()
        log.info(f"portal_user_added cp_id={cp_id} email={email_norm}")
        return RedirectResponse(url=f"/borrower/counterparties/{cp_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"portal_user_add_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to add portal user: {e}")
    finally:
        session.close()


@router.post("/portal-users/{pu_id}/lock")
def borrower_portal_user_lock(request: Request, pu_id: int, reason: str = Form("")):
    """Lock a portal user (no delete — keeps audit trail)."""
    from datetime import datetime
    session = BrunoSession()
    try:
        pu = session.query(PortalUser).filter_by(id=pu_id).first()
        if not pu:
            raise HTTPException(status_code=404, detail=f"Portal user {pu_id} not found")
        before = snapshot(pu)
        pu.locked_at = datetime.utcnow()
        pu.locked_reason = reason.strip() or "manual lock"
        # Also invalidate any active sessions for this user
        from src.borrower.models import PortalSession
        session.query(PortalSession).filter_by(portal_user_id=pu.id).delete()
        write_audit(session, action="lock", entity_type="PortalUser", entity_id=pu.id,
                    before=before, after=snapshot(pu), request=request, notes=reason or None)
        session.commit()
        return RedirectResponse(url=f"/borrower/counterparties/{pu.counterparty_id}", status_code=303)
    finally:
        session.close()


@router.post("/portal-users/{pu_id}/unlock")
def borrower_portal_user_unlock(request: Request, pu_id: int):
    """Unlock a previously locked portal user."""
    session = BrunoSession()
    try:
        pu = session.query(PortalUser).filter_by(id=pu_id).first()
        if not pu:
            raise HTTPException(status_code=404, detail=f"Portal user {pu_id} not found")
        before = snapshot(pu)
        pu.locked_at = None
        pu.locked_reason = None
        write_audit(session, action="unlock", entity_type="PortalUser", entity_id=pu.id,
                    before=before, after=snapshot(pu), request=request)
        session.commit()
        return RedirectResponse(url=f"/borrower/counterparties/{pu.counterparty_id}", status_code=303)
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
    is_nlv_collateralized: bool = Form(False),
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
            is_nlv_collateralized=is_nlv_collateralized,
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
    session = BrunoSession()
    try:
        # Count of counterparties that have ≥1 active loan as lender. Same
        # definition as the lender-admin page. Soft-gate banner shows when
        # approaching LEGAL_CONTEXT.md rule #3 ceiling (~20 active lenders).
        all_cps = session.query(Counterparty).filter(Counterparty.type != CounterpartyType.INTERNAL).all()
        active_lender_count = 0
        for cp in all_cps:
            if any(ln.status == LoanStatus.ACTIVE for ln in cp.loans_as_lender):
                active_lender_count += 1
        return templates.TemplateResponse("borrower_counterparty_new.html", {
            "request": request,
            "counterparty_types": [t.value for t in CounterpartyType],
            "counterparty_tiers": [t.value for t in CounterpartyTier],
            "active_lender_count": active_lender_count,
            "limit_amber": 18,
            "limit_red": 20,
        })
    finally:
        session.close()


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
    merit_account_id: str = Form(""),
    kyc_status: str = Form("not_required"),
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
            merit_account_id=merit_account_id.strip() or None,
            kyc_status=kyc_status.strip() or "not_required",
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
    merit_account_id: str = Form(""),
    kyc_status: str = Form("not_required"),
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
        cp.merit_account_id = merit_account_id.strip() or None
        cp.kyc_status = kyc_status.strip() or "not_required"
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


@router.post("/loans/{loan_id}/approve")
def borrower_loan_approve(request: Request, loan_id: int, notes: str = Form("")):
    """Record the calling principal's approval on a loan (for the quorum gate)."""
    from datetime import datetime
    from src.borrower.quorum import has_approved
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")
        principal = current_principal(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="Login required")
        # principal here is an AuthedPrincipal snapshot; look up the DB row
        pu = session.query(PrincipalUser).filter_by(id=principal.id).first()
        if pu is None:
            raise HTTPException(status_code=401, detail="Principal not found")
        if has_approved(loan_id, pu.id, session=session):
            raise HTTPException(status_code=400, detail="You have already approved this loan")

        appr = LoanApproval(
            loan_id=loan_id,
            approver_id=pu.id,
            approved_at=datetime.utcnow(),
            notes=notes.strip() or None,
        )
        session.add(appr)
        session.flush()
        write_audit(session, action="approve", entity_type="LoanApproval", entity_id=appr.id,
                    after=snapshot(appr), request=request,
                    notes=f"loan {loan_id} approved by {pu.email}")
        session.commit()
        log.info(f"loan_approved loan_id={loan_id} approver={pu.email}")
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"loan_approve_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to approve: {e}")
    finally:
        session.close()


@router.post("/loans/{loan_id}/approve/{approval_id}/revoke")
def borrower_loan_approval_revoke(request: Request, loan_id: int, approval_id: int):
    """A principal may revoke their own approval before activation (e.g. on
    second thought). Cannot revoke another principal's approval."""
    session = BrunoSession()
    try:
        appr = session.query(LoanApproval).filter_by(id=approval_id, loan_id=loan_id).first()
        if not appr:
            raise HTTPException(status_code=404, detail="Approval not found")
        principal = current_principal(request)
        if principal is None or principal.id != appr.approver_id:
            raise HTTPException(status_code=403, detail="Cannot revoke another principal's approval")
        before = snapshot(appr)
        session.delete(appr)
        write_audit(session, action="revoke_approval", entity_type="LoanApproval",
                    entity_id=approval_id, before=before, request=request)
        session.commit()
        log.info(f"loan_approval_revoked loan_id={loan_id} approver_id={appr.approver_id}")
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    finally:
        session.close()


@router.post("/loans/{loan_id}/documents")
async def borrower_document_upload(
    request: Request,
    loan_id: int,
    document_type: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    """Attach a document (typically a signed PDF) to a loan."""
    from src.borrower.documents import DocumentValidationError, store_upload
    session = BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            raise HTTPException(status_code=404, detail=f"Loan {loan_id} not found")
        try:
            dtype = DocumentType(document_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown document_type: {document_type}")

        content = await file.read()
        try:
            stored = store_upload(loan_id, content=content,
                                  filename=file.filename or "upload.pdf",
                                  mime_type=file.content_type or "")
        except DocumentValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        principal = current_principal(request)
        doc = LoanDocument(
            loan_id=loan_id,
            document_type=dtype,
            filename=file.filename or "upload.pdf",
            storage_path=stored.storage_path,
            sha256_hash=stored.sha256_hash,
            size_bytes=stored.size_bytes,
            mime_type=stored.mime_type,
            description=description.strip() or None,
            uploaded_by=(principal.email if principal else "unknown"),
        )
        session.add(doc)
        session.flush()
        write_audit(session, action="create", entity_type="LoanDocument", entity_id=doc.id,
                    after=snapshot(doc), request=request)
        # Mirror into Loan.agreement_document_path for the canonical agreement.
        # This keeps the "single canonical signed PDF" pointer up to date for
        # quick lookups; if multiple agreements are attached, the latest wins.
        if dtype == DocumentType.AGREEMENT:
            loan.agreement_document_path = stored.storage_path
        session.commit()
        log.info(f"loan_document_uploaded loan_id={loan_id} doc_id={doc.id} type={document_type} size={stored.size_bytes}")
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"loan_document_upload_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to upload: {e}")
    finally:
        session.close()


@router.get("/loans/{loan_id}/documents/{doc_id}")
def borrower_document_download(request: Request, loan_id: int, doc_id: int):
    """Download a loan document."""
    from src.borrower.documents import read_for_download
    session = BrunoSession()
    try:
        doc = session.query(LoanDocument).filter_by(id=doc_id, loan_id=loan_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        path = read_for_download(doc.storage_path)
        if path is None:
            raise HTTPException(status_code=404, detail="File missing on disk")
        return FileResponse(str(path), media_type=doc.mime_type, filename=doc.filename)
    finally:
        session.close()


@router.post("/loans/{loan_id}/documents/{doc_id}/delete")
def borrower_document_delete(request: Request, loan_id: int, doc_id: int):
    """Delete a loan document (file + DB row)."""
    from src.borrower.documents import delete_file
    session = BrunoSession()
    try:
        doc = session.query(LoanDocument).filter_by(id=doc_id, loan_id=loan_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        before = snapshot(doc)
        path = doc.storage_path
        was_agreement = doc.document_type == DocumentType.AGREEMENT

        session.delete(doc)
        write_audit(session, action="delete", entity_type="LoanDocument", entity_id=doc_id,
                    before=before, request=request)

        # If we just removed the canonical agreement pointer, clear it.
        # (If another AGREEMENT doc remains, point at the most recent.)
        if was_agreement:
            loan = session.query(Loan).filter_by(id=loan_id).first()
            if loan:
                next_agreement = session.query(LoanDocument).filter_by(
                    loan_id=loan_id, document_type=DocumentType.AGREEMENT,
                ).order_by(LoanDocument.uploaded_at.desc()).first()
                loan.agreement_document_path = next_agreement.storage_path if next_agreement else None

        session.commit()
        # Best-effort file removal — if it fails, DB row is already gone; not catastrophic.
        delete_file(path)
        log.info(f"loan_document_deleted loan_id={loan_id} doc_id={doc_id}")
        return RedirectResponse(url=f"/borrower/loans/{loan_id}", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        log.error(f"loan_document_delete_error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to delete: {e}")
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
