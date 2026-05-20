#!/usr/bin/env python3
"""
Seed Bruno's lender-portal pilot users.

Idempotent — safe to re-run. Creates the Hologram OÜ shareholder placeholder
counterparty if missing, then seeds portal_users rows for the four pilot users
attached to the right lender entities.

Pre-requisites:
- bruno.db exists (run `python -c "from src.borrower.models import init_db; init_db()"`)
- The four shareholder lender counterparties already exist by name:
  Thirona Capital OÜ, SK4 HoldCo OÜ, Waddy Holding OÜ, Arvutitugi OÜ
  (these are created by the upstream borrower seed scripts — not by this file)

What this script does:
- Creates Hologram OÜ (Rasmus's shareholder placeholder, no loans) if missing
- Seeds portal_users:
    rain.rosimannus@gmail.com × {Thirona, SK4 HoldCo, Waddy}
    rain.rosimannus@mac.com   × {Thirona, SK4 HoldCo, Waddy}
    lauriluik1982@gmail.com   × {Arvutitugi}
    rasmus.rosimannus@gmail.com × {Hologram}

Usage:
    python tools/seed_pilot.py
    python tools/seed_pilot.py --dry-run    # show what would happen, don't write
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Make src/ importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.borrower.models import (  # noqa: E402
    Counterparty, CounterpartyTier, CounterpartyType, PortalUser, get_session_factory,
)


# (email, lender_entity_name) pairs to seed
SEED: list[tuple[str, str]] = [
    ("rain.rosimannus@gmail.com",   "Thirona Capital OÜ"),
    ("rain.rosimannus@gmail.com",   "SK4 HoldCo OÜ"),
    ("rain.rosimannus@gmail.com",   "Waddy Holding OÜ"),
    ("rain.rosimannus@mac.com",     "Thirona Capital OÜ"),
    ("rain.rosimannus@mac.com",     "SK4 HoldCo OÜ"),
    ("rain.rosimannus@mac.com",     "Waddy Holding OÜ"),
    ("lauriluik1982@gmail.com",     "Arvutitugi OÜ"),
    ("rasmus.rosimannus@gmail.com", "Hologram OÜ"),
]


# Counterparty record for Hologram OÜ — created here if missing.
HOLOGRAM = dict(
    name="Hologram OÜ",
    type=CounterpartyType.COMPANY,
    tier=CounterpartyTier.SHAREHOLDER,
    country="EE",
    legal_form="OÜ",
    kyc_status="not_required",
    notes=(
        "Rasmus Rosimannus — shareholder via sweat equity. Placeholder lender "
        "entity; no loans on file. Portal access for UAT + forward compatibility "
        "if Hologram ever lends to MesiCap."
    ),
)


def main(dry_run: bool = False) -> int:
    sess = get_session_factory()()
    created_cp = 0
    created_pu = 0
    skipped_pu = 0
    missing_entities: list[str] = []

    try:
        # 1) Hologram OÜ — create if missing
        holo = sess.query(Counterparty).filter_by(name=HOLOGRAM["name"]).first()
        if holo is None:
            if dry_run:
                print(f"would create counterparty: {HOLOGRAM['name']}")
            else:
                holo = Counterparty(**HOLOGRAM)
                sess.add(holo)
                sess.commit()
                sess.refresh(holo)
                print(f"created counterparty: cp#{holo.id} {holo.name}")
                created_cp += 1
        else:
            print(f"counterparty exists: cp#{holo.id} {holo.name}")

        # 2) For each (email, entity_name) pair, ensure a portal_user row exists
        for email, entity_name in SEED:
            cp = sess.query(Counterparty).filter_by(name=entity_name).first()
            if cp is None:
                missing_entities.append(entity_name)
                continue
            existing = sess.query(PortalUser).filter_by(
                email=email, counterparty_id=cp.id
            ).first()
            if existing is not None:
                print(f"  skip (exists): {email:36s} → {entity_name}")
                skipped_pu += 1
                continue
            if dry_run:
                print(f"  would seed:    {email:36s} → {entity_name}")
                continue
            sess.add(PortalUser(
                counterparty_id=cp.id,
                email=email,
                invited_by="seed_pilot",
                invitation_date=datetime.utcnow(),
            ))
            print(f"  seeded:        {email:36s} → {entity_name}")
            created_pu += 1

        if not dry_run:
            sess.commit()

        # 3) Report
        print()
        print(f"summary: created {created_cp} counterparty, "
              f"seeded {created_pu} portal_users, skipped {skipped_pu} existing")
        if missing_entities:
            unique = sorted(set(missing_entities))
            print()
            print("WARNING: the following lender entities do not exist in bruno.db:")
            for n in unique:
                print(f"  - {n}")
            print()
            print("Run the upstream borrower seed scripts first to create them, then re-run this.")
            return 2
        return 0
    finally:
        sess.close()


if __name__ == "__main__":
    rc = main(dry_run="--dry-run" in sys.argv)
    sys.exit(rc)
