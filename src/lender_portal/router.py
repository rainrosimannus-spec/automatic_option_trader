"""
Lender portal FastAPI router. Mounted at /lenders/ in the main app.

Read-only, magic-link authenticated. See docs/governance.md §5 and
CLAUDE.md "Lender portal privacy" for the architectural invariants this
module enforces.

What this router is allowed to read:
- The authenticated user's own Counterparty record
- Loans where Counterparty is the lender (loans_as_lender)
- Movements + payments on those loans
- Statement files in data/statements/{counterparty_id}/

What this router is forbidden from reading:
- Any other counterparty's data
- Any Maggy/Winston tables (positions, account, trades, etc.)
- The audit log
- System config

The single allowed exception is via src/borrower/collateral.py:collateral_view(loan_id)
for loans where loan.is_nlv_collateralized=True (see governance.md §5.3).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.borrower.accrual import compute_accrual
from src.borrower.collateral import collateral_view, is_collateral_viewable
from src.borrower.models import (
    ContactUpdateRequest, DocumentType, Loan, LoanDocument, LoanMovement,
    LoanStatus, MovementType, Payment, PaymentStatus, PortalUser,
    get_session_factory,
)
from src.lender_portal.auth import (
    AuthedUser, SESSION_COOKIE, clear_session, consume_magic_link, current_user,
    request_magic_link, set_session_cookie,
)


router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_BrunoSession = get_session_factory()


# === Helpers ===

def _require_user(request: Request) -> AuthedUser:
    """Resolve session → user or raise to redirect to /lenders/login."""
    user = current_user(request)
    if user is None:
        # 401 here would force the caller to handle; cleaner to raise
        # and let the outer wrapper convert to a redirect.
        raise _NeedsLogin()
    return user


class _NeedsLogin(Exception):
    pass


def _require_loan_owned_by_user(loan_id: int, user: AuthedUser, session) -> Loan:
    """
    Single ownership check. Per CLAUDE.md "Lender portal privacy" — the only
    place loan-ownership is verified in this router. A bypass requires editing
    this function, not adding a new route.

    A loan is "owned" by the user iff its lender_id is in the user's
    counterparty access set (one email may have access to multiple entities).
    """
    loan = session.query(Loan).filter_by(id=loan_id).first()
    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.lender_id not in user.counterparty_ids:
        # Don't leak that the loan exists; 404 not 403.
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


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


# === Routes ===

@router.get("/", response_class=HTMLResponse)
def lender_root(request: Request):
    """Root: redirect to dashboard if logged in, else login page."""
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/lenders/login", status_code=303)
    return RedirectResponse(url="/lenders/dashboard", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def lender_login_form(request: Request):
    if current_user(request) is not None:
        return RedirectResponse(url="/lenders/dashboard", status_code=303)
    return templates.TemplateResponse("lender_login.html", {
        "request": request,
        "message": None,
        "email_value": "",
    })


@router.post("/login", response_class=HTMLResponse)
def lender_login_submit(request: Request, email: str = Form(...)):
    result = request_magic_link(email, request=request)
    # Always show a generic message in prod; dev surfaces the link inline below.
    message = (
        "If that email is registered, a magic link has been sent. "
        "Check your inbox; the link is valid for 15 minutes."
    )
    if result.get("status") == "rate_limited":
        message = "Too many requests in a short window — please wait a few minutes before trying again."
    elif result.get("status") == "locked":
        message = "This account is currently locked. Contact MesiCap to restore access."
    return templates.TemplateResponse("lender_login.html", {
        "request": request,
        "message": message,
        "email_value": email,
        # Only present in dev mode (see auth.DEV_LOG_MAGIC_LINKS)
        "dev_magic_url": result.get("magic_url"),
    })


@router.get("/magic/{token}")
def lender_magic_consume(request: Request, token: str):
    session_token = consume_magic_link(token, request=request)
    if session_token is None:
        return templates.TemplateResponse("lender_login.html", {
            "request": request,
            "message": "That link is invalid or has expired. Request a new one.",
            "email_value": "",
        }, status_code=400)
    response = RedirectResponse(url="/lenders/dashboard", status_code=303)
    set_session_cookie(response, session_token)
    return response


@router.get("/logout")
def lender_logout(request: Request):
    response = RedirectResponse(url="/lenders/login", status_code=303)
    clear_session(request, response)
    return response


@router.get("/dashboard", response_class=HTMLResponse)
def lender_dashboard(request: Request):
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)

    session = _BrunoSession()
    try:
        from src.borrower.models import Counterparty
        # The user's email may have access to multiple lender entities.
        # Aggregate all active loans across every counterparty in the access set.
        counterparties = (
            session.query(Counterparty)
            .filter(Counterparty.id.in_(user.counterparty_ids))
            .order_by(Counterparty.name)
            .all()
        )
        loans = sorted(
            session.query(Loan).filter(
                Loan.lender_id.in_(user.counterparty_ids),
                Loan.status == LoanStatus.ACTIVE,
            ).all(),
            key=lambda l: l.origination_date,
        )
        # Build a display name: either one entity name, or "Entity A · Entity B · ..."
        viewing_as = " · ".join(c.name for c in counterparties) if counterparties else user.email

        rows = []
        for loan in loans:
            outstanding = _outstanding(loan)
            acc = compute_accrual(loan, date.today())
            # Find next unpaid payment, if any
            pending = [p for p in loan.payments if p.status in (PaymentStatus.SCHEDULED, PaymentStatus.OVERDUE)]
            next_payment = min(pending, key=lambda p: p.scheduled_date) if pending else None
            rows.append({
                "id": loan.id,
                "description": loan.description or loan.contract_reference,
                "currency": loan.currency,
                "outstanding": outstanding,
                "accrued_interest": acc.accrued_interest,
                "total_owed": acc.total_owed,
                "rate_pct": loan.interest_rate_annual * 100,
                "maturity_date": loan.maturity_date,
                "next_payment_date": next_payment.scheduled_date if next_payment else None,
                "next_payment_amount": next_payment.scheduled_amount if next_payment else None,
                "is_nlv_collateralized": loan.is_nlv_collateralized,
            })

        # Build lender-side row for each active loan with its specific entity name
        # so the dashboard can show which entity owns which loan in multi-entity views.
        loan_to_lender = {l.id: l.lender.name for l in loans}
        for r in rows:
            r["lender_name"] = loan_to_lender.get(r["id"])

        return templates.TemplateResponse("lender_dashboard.html", {
            "request": request,
            "user": user,
            "viewing_as": viewing_as,
            "multi_entity": len(counterparties) > 1,
            "loans": rows,
            "as_of": date.today(),
        })
    finally:
        session.close()


@router.get("/loans/{loan_id}", response_class=HTMLResponse)
def lender_loan_detail(request: Request, loan_id: int):
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)

    session = _BrunoSession()
    try:
        loan = _require_loan_owned_by_user(loan_id, user, session)
        outstanding = _outstanding(loan)
        acc = compute_accrual(loan, date.today())
        movements_sorted = sorted(loan.movements, key=lambda m: m.movement_date)
        payments_sorted = sorted(loan.payments, key=lambda p: p.scheduled_date)
        amendments_sorted = sorted(loan.amendments, key=lambda a: a.amendment_date)
        # Lender-visible documents: signed agreement + signed amendments only.
        # Side letters and "other" docs are internal — never surface to lenders.
        documents_visible = sorted(
            session.query(LoanDocument).filter(
                LoanDocument.loan_id == loan.id,
                LoanDocument.document_type.in_([DocumentType.AGREEMENT, DocumentType.AMENDMENT]),
            ).all(),
            key=lambda d: d.uploaded_at,
            reverse=True,
        )
        return templates.TemplateResponse("lender_loan_detail.html", {
            "request": request,
            "user": user,
            "loan": loan,
            "outstanding": outstanding,
            "accrual": acc,
            "movements": movements_sorted,
            "payments": payments_sorted,
            "amendments": amendments_sorted,
            "rate_pct": loan.interest_rate_annual * 100,
            "has_collateral_view": is_collateral_viewable(loan),
            "documents": documents_visible,
        })
    finally:
        session.close()


@router.get("/loans/{loan_id}/documents/{doc_id}")
def lender_document_download(request: Request, loan_id: int, doc_id: int):
    """Lender-side download of a signed agreement or amendment on their own
    loan. Side letters and 'other' document types are NEVER served on this
    route — they're internal to MesiCap.
    """
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)

    session = _BrunoSession()
    try:
        # First confirm loan ownership — this is the same gate as everywhere else.
        _require_loan_owned_by_user(loan_id, user, session)
        doc = session.query(LoanDocument).filter_by(id=doc_id, loan_id=loan_id).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        # Whitelist: only signed legal documents are lender-visible.
        if doc.document_type not in (DocumentType.AGREEMENT, DocumentType.AMENDMENT):
            raise HTTPException(status_code=404, detail="Document not found")
        from src.borrower.documents import read_for_download
        path = read_for_download(doc.storage_path)
        if path is None:
            raise HTTPException(status_code=404, detail="File missing")
        from fastapi.responses import FileResponse
        return FileResponse(str(path), media_type=doc.mime_type, filename=doc.filename)
    finally:
        session.close()


@router.get("/loans/{loan_id}/collateral", response_class=HTMLResponse)
def lender_loan_collateral(request: Request, loan_id: int):
    """Collateral view — only shown if loan.is_nlv_collateralized (governance.md §5.3)."""
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)

    session = _BrunoSession()
    try:
        loan = _require_loan_owned_by_user(loan_id, user, session)
        if not is_collateral_viewable(loan):
            # 404, not 403 — don't leak that the flag exists on other loans
            raise HTTPException(status_code=404, detail="Not found")
        view = collateral_view(loan_id)
        return templates.TemplateResponse("lender_collateral.html", {
            "request": request,
            "user": user,
            "loan": loan,
            "view": view,
        })
    finally:
        session.close()


@router.get("/statements", response_class=HTMLResponse)
def lender_statements(request: Request):
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)
    from src.borrower.statements import list_statements
    from src.borrower.models import Counterparty
    session = _BrunoSession()
    try:
        statements = []
        cp_names = {
            c.id: c.name for c in session.query(Counterparty)
            .filter(Counterparty.id.in_(user.counterparty_ids)).all()
        }
        for cp_id in user.counterparty_ids:
            for s in list_statements(cp_id):
                statements.append({
                    "period": f"{s['year']} Q{s['quarter']}",
                    "issued_at": s["mtime"],
                    "size_kb": s["size_bytes"] // 1024,
                    "download_url": f"/lenders/statements/{cp_id}/{s['filename']}",
                    "counterparty_name": cp_names.get(cp_id, ""),
                })
        # Sort newest issue first
        statements.sort(key=lambda x: x["issued_at"], reverse=True)
        return templates.TemplateResponse("lender_statements.html", {
            "request": request,
            "user": user,
            "statements": statements,
            "multi_entity": len(user.counterparty_ids) > 1,
        })
    finally:
        session.close()


@router.get("/statements/{cp_id}/{filename}")
def lender_statement_download(request: Request, cp_id: int, filename: str):
    """Download a statement PDF. Ownership check: cp_id must be in the user's
    access set, AND filename must match the expected pattern (no traversal)."""
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)
    if cp_id not in user.counterparty_ids:
        raise HTTPException(status_code=404, detail="Not found")
    # Defense in depth: only allow YYYY-Q[1-4].pdf filenames; reject path
    # traversal and any other shape.
    import re
    if not re.fullmatch(r"\d{4}-Q[1-4]\.pdf", filename):
        raise HTTPException(status_code=404, detail="Not found")
    from src.borrower.statements import STATEMENTS_DIR
    path = STATEMENTS_DIR / str(cp_id) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    from fastapi.responses import FileResponse
    return FileResponse(str(path), media_type="application/pdf", filename=filename)


@router.get("/contact", response_class=HTMLResponse)
def lender_contact(request: Request):
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)
    session = _BrunoSession()
    try:
        from src.borrower.models import Counterparty
        counterparties = (
            session.query(Counterparty)
            .filter(Counterparty.id.in_(user.counterparty_ids))
            .order_by(Counterparty.name)
            .all()
        )
        return templates.TemplateResponse("lender_contact.html", {
            "request": request,
            "user": user,
            "counterparties": counterparties,
            "submitted": False,
        })
    finally:
        session.close()


@router.post("/contact/request", response_class=HTMLResponse)
def lender_contact_submit(
    request: Request,
    subject: str = Form(...),
    message: str = Form(...),
):
    """Lender submits a contact-info update request. Writes a ContactUpdateRequest
    row + audit_log entry; admins pick it up on /borrower/."""
    try:
        user = _require_user(request)
    except _NeedsLogin:
        return RedirectResponse(url="/lenders/login", status_code=303)
    subject = (subject or "").strip()[:255]
    message = (message or "").strip()
    if not subject or not message:
        raise HTTPException(status_code=400, detail="Subject and message are required.")
    if len(message) > 5000:
        raise HTTPException(status_code=400, detail="Message too long (5000 char limit).")

    session = _BrunoSession()
    try:
        from src.borrower.audit import snapshot, write_audit
        pu = session.query(PortalUser).filter_by(id=user.id).first()
        if pu is None:
            raise HTTPException(status_code=401, detail="Not logged in")
        # Bind to whichever counterparty in the user's set is the primary
        # (lowest cp_id is fine — admin can re-route later via admin_notes).
        cp_id = min(user.counterparty_ids) if user.counterparty_ids else pu.counterparty_id
        req_row = ContactUpdateRequest(
            portal_user_id=pu.id,
            counterparty_id=cp_id,
            subject=subject,
            message=message,
            status="new",
        )
        session.add(req_row)
        session.flush()
        write_audit(session, action="create", entity_type="ContactUpdateRequest",
                    entity_id=req_row.id, after=snapshot(req_row), request=request,
                    actor=f"portal:{pu.email}")
        session.commit()

        from src.borrower.models import Counterparty
        counterparties = (
            session.query(Counterparty)
            .filter(Counterparty.id.in_(user.counterparty_ids))
            .order_by(Counterparty.name)
            .all()
        )
        return templates.TemplateResponse("lender_contact.html", {
            "request": request,
            "user": user,
            "counterparties": counterparties,
            "submitted": True,
        })
    finally:
        session.close()
