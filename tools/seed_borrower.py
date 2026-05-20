#!/usr/bin/env python3
"""
Idempotent Bruno borrower seed: reads `tools/seed_borrower_data.json` (produced
by `tools/gen_seed_borrower.py`) and populates an empty (or existing) bruno.db.

Designed for the one-time cutover where Rasmus's MesiCap-clone bruno.db needs
the same counterparties / loans / movements / amendments / payments / portal
users / principal users as Rain's live dev DB. Safe to re-run; existing rows
are detected by natural key and skipped.

Order of insertion (dependency-driven):
  1. Counterparty           (natural key: name)
  2. Loan                   (natural key: contract_reference; FKs by counterparty.name)
  3. LoanMovement           (FK by loan.contract_reference)
  4. LoanAmendment          (FK by loan.contract_reference)
  5. Payment                (FK by loan.contract_reference)
  6. PortalUser             (natural key: (email, counterparty.id); FK by counterparty.name)
  7. PrincipalUser          (natural key: email)

Not seeded (runtime/derived):
  interest_accruals, audit_log, *_sessions, bank_transactions, headroom_inputs,
  merit_balances, loan_documents, loan_approvals, contact_update_requests

Pre-requisite: bruno.db schema must exist. If empty, run first:
    python -c "from src.borrower.models import init_db; init_db()"

Usage:
    python tools/seed_borrower.py
    python tools/seed_borrower.py --dry-run    # report planned inserts, write nothing
    python tools/seed_borrower.py --data PATH  # custom JSON path
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

# Make src/ importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.borrower.models import (  # noqa: E402
    Counterparty, CounterpartyType, CounterpartyTier,
    Loan, LoanType, RepaymentStructure, LoanPurpose, InterestRateType,
    DayCountConvention, InterestTreatment, PaymentFrequency, LoanStatus,
    LoanMovement, MovementType,
    LoanAmendment,
    Payment, PaymentType, PaymentStatus,
    PortalUser, PrincipalUser,
    get_session_factory,
)

DEFAULT_DATA = Path(__file__).resolve().parent / "seed_borrower_data.json"


# ── Enum hydration map ────────────────────────────────────────────────
# field-name -> enum class. Applied uniformly across rows when present.
COUNTERPARTY_ENUMS = {
    "type": CounterpartyType,
    "tier": CounterpartyTier,
}
LOAN_ENUMS = {
    "loan_type": LoanType,
    "repayment_structure": RepaymentStructure,
    "purpose": LoanPurpose,
    "interest_rate_type": InterestRateType,
    "day_count_convention": DayCountConvention,
    "interest_treatment": InterestTreatment,
    "payment_frequency": PaymentFrequency,
    "status": LoanStatus,
}
MOVEMENT_ENUMS = {"movement_type": MovementType}
PAYMENT_ENUMS = {"payment_type": PaymentType, "status": PaymentStatus}

DATE_FIELDS = {
    "kyc_completed_at": datetime,
    "created_at": datetime,
    "updated_at": datetime,
    "invitation_date": datetime,
    "contract_date": date,
    "origination_date": date,
    "maturity_date": date,
    "movement_date": date,
    "amendment_date": date,
    "scheduled_date": date,
    "paid_date": date,
}


def _to_dt(value: str | None, kind):
    if value is None:
        return None
    if kind is datetime:
        return datetime.fromisoformat(value)
    return date.fromisoformat(value)


def _hydrate(row: dict, enum_map: dict[str, type]) -> dict:
    """Return a copy of `row` with enum strings replaced by enum members and
    ISO date strings replaced by date/datetime objects. Drops keys whose value
    is None ONLY if the model field allows None (we just pass them through —
    SQLAlchemy handles None).
    """
    out = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
            continue
        if k in enum_map:
            out[k] = enum_map[k](v)
        elif k in DATE_FIELDS:
            out[k] = _to_dt(v, DATE_FIELDS[k])
        else:
            out[k] = v
    return out


def _diff_for_report(existing, planned: dict, fields: list[str]) -> list[str]:
    diffs = []
    for f in fields:
        cur = getattr(existing, f, None)
        new = planned.get(f)
        # Normalize enum members vs string-values for comparison
        cur_v = cur.value if hasattr(cur, "value") else cur
        new_v = new.value if hasattr(new, "value") else new
        if cur_v != new_v:
            diffs.append(f"{f}: {cur_v!r} -> {new_v!r}")
    return diffs


def seed(data: dict, dry_run: bool = False) -> int:
    Session = get_session_factory()
    counters = {"counterparties": 0, "loans": 0, "loan_movements": 0,
                "loan_amendments": 0, "payments": 0,
                "portal_users": 0, "principal_users": 0}
    skipped = {k: 0 for k in counters}

    with Session() as sess:
        # 1. Counterparties (PK natural: name)
        cp_by_name: dict[str, Counterparty] = {
            cp.name: cp for cp in sess.query(Counterparty).all()
        }
        for row in data["counterparties"]:
            name = row["name"]
            if name in cp_by_name:
                skipped["counterparties"] += 1
                continue
            cp = Counterparty(**_hydrate(row, COUNTERPARTY_ENUMS))
            if not dry_run:
                sess.add(cp)
                sess.flush()
            cp_by_name[name] = cp
            counters["counterparties"] += 1

        # 2. Loans (PK natural: contract_reference; FK: counterparty name)
        loan_by_ref: dict[str, Loan] = {
            l.contract_reference: l
            for l in sess.query(Loan).all()
            if l.contract_reference
        }
        for row in data["loans"]:
            ref = row["contract_reference"]
            if ref in loan_by_ref:
                skipped["loans"] += 1
                continue
            lender = cp_by_name.get(row["lender_name"])
            borrower = cp_by_name.get(row["borrower_name"])
            if lender is None or borrower is None:
                raise RuntimeError(
                    f"Loan {ref!r} FK resolution failed: "
                    f"lender={row['lender_name']!r} borrower={row['borrower_name']!r}"
                )
            payload = _hydrate(row, LOAN_ENUMS)
            payload.pop("lender_name", None)
            payload.pop("borrower_name", None)
            loan = Loan(lender_id=lender.id, borrower_id=borrower.id, **payload)
            if not dry_run:
                sess.add(loan)
                sess.flush()
            loan_by_ref[ref] = loan
            counters["loans"] += 1

        # 3. LoanMovement (composite natural key)
        existing_mov_keys = {
            (m.loan_id, m.movement_date, (m.movement_type.value if m.movement_type else None), m.amount)
            for m in sess.query(LoanMovement).all()
        }
        for row in data["loan_movements"]:
            loan = loan_by_ref.get(row["loan_contract_reference"])
            if loan is None:
                raise RuntimeError(
                    f"LoanMovement FK resolution failed: loan ref "
                    f"{row['loan_contract_reference']!r}"
                )
            payload = _hydrate(row, MOVEMENT_ENUMS)
            payload.pop("loan_contract_reference", None)
            key = (loan.id, payload["movement_date"],
                   (payload["movement_type"].value if payload["movement_type"] else None),
                   payload["amount"])
            if key in existing_mov_keys:
                skipped["loan_movements"] += 1
                continue
            if not dry_run:
                sess.add(LoanMovement(loan_id=loan.id, **payload))
            existing_mov_keys.add(key)
            counters["loan_movements"] += 1

        # 4. LoanAmendment (composite natural key)
        existing_amd_keys = {
            (a.loan_id, a.amendment_date, a.field_changed)
            for a in sess.query(LoanAmendment).all()
        }
        for row in data["loan_amendments"]:
            loan = loan_by_ref.get(row["loan_contract_reference"])
            if loan is None:
                raise RuntimeError(
                    f"LoanAmendment FK resolution failed: loan ref "
                    f"{row['loan_contract_reference']!r}"
                )
            payload = _hydrate(row, {})
            payload.pop("loan_contract_reference", None)
            key = (loan.id, payload["amendment_date"], payload["field_changed"])
            if key in existing_amd_keys:
                skipped["loan_amendments"] += 1
                continue
            if not dry_run:
                sess.add(LoanAmendment(loan_id=loan.id, **payload))
            existing_amd_keys.add(key)
            counters["loan_amendments"] += 1

        # 5. Payment (composite natural key)
        existing_pay_keys = {
            (p.loan_id, p.scheduled_date,
             (p.payment_type.value if p.payment_type else None))
            for p in sess.query(Payment).all()
        }
        for row in data["payments"]:
            loan = loan_by_ref.get(row["loan_contract_reference"])
            if loan is None:
                raise RuntimeError(
                    f"Payment FK resolution failed: loan ref "
                    f"{row['loan_contract_reference']!r}"
                )
            payload = _hydrate(row, PAYMENT_ENUMS)
            payload.pop("loan_contract_reference", None)
            key = (loan.id, payload["scheduled_date"],
                   (payload["payment_type"].value if payload["payment_type"] else None))
            if key in existing_pay_keys:
                skipped["payments"] += 1
                continue
            if not dry_run:
                sess.add(Payment(loan_id=loan.id, **payload))
            existing_pay_keys.add(key)
            counters["payments"] += 1

        # 6. PortalUser (composite natural key: email + counterparty)
        existing_pu_keys = {
            (u.email, u.counterparty_id)
            for u in sess.query(PortalUser).all()
        }
        for row in data["portal_users"]:
            cp = cp_by_name.get(row["counterparty_name"])
            if cp is None:
                raise RuntimeError(
                    f"PortalUser FK resolution failed: counterparty "
                    f"{row['counterparty_name']!r}"
                )
            payload = _hydrate(row, {})
            payload.pop("counterparty_name", None)
            key = (payload["email"], cp.id)
            if key in existing_pu_keys:
                skipped["portal_users"] += 1
                continue
            if not dry_run:
                sess.add(PortalUser(counterparty_id=cp.id, **payload))
            existing_pu_keys.add(key)
            counters["portal_users"] += 1

        # 7. PrincipalUser (unique email)
        existing_principal_emails = {
            u.email for u in sess.query(PrincipalUser).all()
        }
        for row in data["principal_users"]:
            if row["email"] in existing_principal_emails:
                skipped["principal_users"] += 1
                continue
            payload = _hydrate(row, {})
            if not dry_run:
                sess.add(PrincipalUser(**payload))
            existing_principal_emails.add(row["email"])
            counters["principal_users"] += 1

        if dry_run:
            sess.rollback()
        else:
            sess.commit()

    label = "DRY-RUN: would insert" if dry_run else "Inserted"
    print(f"{label}:")
    for k in counters:
        print(f"  {k:20s} +{counters[k]}  (skipped {skipped[k]})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help=f"Path to seed JSON (default: {DEFAULT_DATA})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be inserted; write nothing.")
    args = ap.parse_args()

    if not args.data.exists():
        print(f"ERROR: seed data not found: {args.data}", file=sys.stderr)
        print("  Run `python tools/gen_seed_borrower.py` on the source DB first.",
              file=sys.stderr)
        return 2

    data = json.loads(args.data.read_text())
    schema_version = data.get("schema_version")
    if schema_version != 1:
        print(f"ERROR: unsupported seed schema_version: {schema_version}",
              file=sys.stderr)
        return 2

    return seed(data, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
