#!/usr/bin/env python3
"""
One-time exporter: dump the Bruno borrower data set from the live bruno.db
into a JSON file that `tools/seed_borrower.py` can replay against any empty
bruno.db (e.g. Rasmus's MesiCap-clone DB).

Resolves IBKR-ish primary-key references to natural keys so the JSON is
ID-stable across regenerations:
  - Counterparty.name              for the lender/borrower FKs
  - Loan.contract_reference        for movements/amendments/payments
  - email                          for portal/principal users
  - (email, counterparty_name)     for portal_users (composite)

Skipped (regenerated at runtime, not seedable):
  interest_accruals     (daily accrual job repopulates)
  audit_log             (history)
  portal_sessions, principal_sessions (auth state)
  bank_transactions, headroom_inputs, merit_balances, loan_documents,
  loan_approvals, contact_update_requests

Sensitive runtime fields stripped from portal/principal users:
  magic_link_token_hash, magic_link_expires_at, magic_link_sent_at,
  last_login_at, locked_at, locked_reason

Usage:
    python tools/gen_seed_borrower.py
        -> writes tools/seed_borrower_data.json

Idempotent. Safe to re-run; overwrites the JSON.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

# Make src/ importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.borrower.models import (  # noqa: E402
    Counterparty, Loan, LoanMovement, LoanAmendment, Payment,
    PortalUser, PrincipalUser, get_session_factory,
)

OUTPUT = Path(__file__).resolve().parent / "seed_borrower_data.json"


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _enum(value):
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


def export_counterparty(cp: Counterparty) -> dict:
    return {
        "name": cp.name,
        "type": _enum(cp.type),
        "tier": _enum(cp.tier),
        "legal_form": cp.legal_form,
        "registration_number": cp.registration_number,
        "country": cp.country,
        "address": cp.address,
        "contact_email": cp.contact_email,
        "contact_phone": cp.contact_phone,
        "iban": cp.iban,
        "secondary_iban": cp.secondary_iban,
        "notes": cp.notes,
        "related_principal": cp.related_principal,
        "kyc_status": cp.kyc_status,
        "kyc_completed_at": _iso(cp.kyc_completed_at),
        "merit_account_id": cp.merit_account_id,
        "created_at": _iso(cp.created_at),
        "updated_at": _iso(cp.updated_at),
    }


def export_loan(loan: Loan, id_to_name: dict[int, str]) -> dict:
    return {
        "lender_name": id_to_name[loan.lender_id],
        "borrower_name": id_to_name[loan.borrower_id],
        "contract_reference": loan.contract_reference,
        "description": loan.description,
        "loan_type": _enum(loan.loan_type),
        "repayment_structure": _enum(loan.repayment_structure),
        "purpose": _enum(loan.purpose),
        "principal_max": loan.principal_max,
        "currency": loan.currency,
        "interest_rate_type": _enum(loan.interest_rate_type),
        "interest_rate_annual": loan.interest_rate_annual,
        "floating_benchmark": loan.floating_benchmark,
        "floating_spread": loan.floating_spread,
        "day_count_convention": _enum(loan.day_count_convention),
        "interest_treatment": _enum(loan.interest_treatment),
        "payment_frequency": _enum(loan.payment_frequency),
        "payment_day_of_month": loan.payment_day_of_month,
        "installment_amount": loan.installment_amount,
        "contract_date": _iso(loan.contract_date),
        "origination_date": _iso(loan.origination_date),
        "maturity_date": _iso(loan.maturity_date),
        "collateral_description": loan.collateral_description,
        "parent_loan_description": loan.parent_loan_description,
        "is_subordinated": loan.is_subordinated,
        "early_repayment_allowed": loan.early_repayment_allowed,
        "early_repayment_notice_days": loan.early_repayment_notice_days,
        "status": _enum(loan.status),
        "agreement_document_path": loan.agreement_document_path,
        "notes": loan.notes,
        "is_nlv_collateralized": getattr(loan, "is_nlv_collateralized", False),
        "created_at": _iso(loan.created_at),
        "updated_at": _iso(loan.updated_at),
    }


def export_movement(m: LoanMovement, loan_id_to_ref: dict[int, str]) -> dict:
    return {
        "loan_contract_reference": loan_id_to_ref[m.loan_id],
        "movement_date": _iso(m.movement_date),
        "movement_type": _enum(m.movement_type),
        "amount": m.amount,
        "currency": m.currency,
        "bank_reference": m.bank_reference,
        "bank_account_iban": m.bank_account_iban,
        "description": m.description,
        "notes": m.notes,
        "created_at": _iso(m.created_at),
    }


def export_amendment(a: LoanAmendment, loan_id_to_ref: dict[int, str]) -> dict:
    return {
        "loan_contract_reference": loan_id_to_ref[a.loan_id],
        "amendment_date": _iso(a.amendment_date),
        "field_changed": a.field_changed,
        "old_value": a.old_value,
        "new_value": a.new_value,
        "description": a.description,
        "notes": a.notes,
        "created_at": _iso(a.created_at),
    }


def export_payment(p: Payment, loan_id_to_ref: dict[int, str]) -> dict:
    return {
        "loan_contract_reference": loan_id_to_ref[p.loan_id],
        "scheduled_date": _iso(p.scheduled_date),
        "scheduled_amount": p.scheduled_amount,
        "payment_type": _enum(p.payment_type),
        "scheduled_principal_component": p.scheduled_principal_component,
        "scheduled_interest_component": p.scheduled_interest_component,
        "paid_date": _iso(p.paid_date),
        "paid_amount": p.paid_amount,
        "bank_reference": p.bank_reference,
        "status": _enum(p.status),
        "notes": p.notes,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }


def export_portal_user(u: PortalUser, cp_id_to_name: dict[int, str]) -> dict:
    return {
        "email": u.email,
        "counterparty_name": cp_id_to_name[u.counterparty_id],
        "invited_by": u.invited_by,
        "invitation_date": _iso(u.invitation_date),
        "created_at": _iso(u.created_at),
        "updated_at": _iso(u.updated_at),
    }


def export_principal_user(u: PrincipalUser) -> dict:
    return {
        "email": u.email,
        "name": u.name,
        "created_at": _iso(u.created_at),
        "updated_at": _iso(u.updated_at),
    }


def main() -> int:
    Session = get_session_factory()
    with Session() as sess:
        counterparties = sess.query(Counterparty).order_by(Counterparty.id).all()
        cp_id_to_name = {cp.id: cp.name for cp in counterparties}

        loans = sess.query(Loan).order_by(Loan.id).all()
        loan_id_to_ref = {l.id: l.contract_reference for l in loans}
        # Sanity: every loan must have a contract_reference (we natural-key on it)
        unrefed = [l.id for l in loans if not l.contract_reference]
        if unrefed:
            print(f"ERROR: loans missing contract_reference (FK natural key): {unrefed}",
                  file=sys.stderr)
            return 2

        movements = sess.query(LoanMovement).order_by(LoanMovement.id).all()
        amendments = sess.query(LoanAmendment).order_by(LoanAmendment.id).all()
        payments = sess.query(Payment).order_by(Payment.id).all()
        portal_users = sess.query(PortalUser).order_by(PortalUser.id).all()
        principal_users = sess.query(PrincipalUser).order_by(PrincipalUser.id).all()

        data = {
            "schema_version": 1,
            "exported_at": datetime.utcnow().isoformat(),
            "counterparties": [export_counterparty(cp) for cp in counterparties],
            "loans": [export_loan(l, cp_id_to_name) for l in loans],
            "loan_movements": [export_movement(m, loan_id_to_ref) for m in movements],
            "loan_amendments": [export_amendment(a, loan_id_to_ref) for a in amendments],
            "payments": [export_payment(p, loan_id_to_ref) for p in payments],
            "portal_users": [export_portal_user(u, cp_id_to_name) for u in portal_users],
            "principal_users": [export_principal_user(u) for u in principal_users],
        }

    OUTPUT.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Wrote {OUTPUT}")
    print(f"  counterparties     {len(data['counterparties'])}")
    print(f"  loans              {len(data['loans'])}")
    print(f"  loan_movements     {len(data['loan_movements'])}")
    print(f"  loan_amendments    {len(data['loan_amendments'])}")
    print(f"  payments           {len(data['payments'])}")
    print(f"  portal_users       {len(data['portal_users'])}")
    print(f"  principal_users    {len(data['principal_users'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
