#!/usr/bin/env python3
"""
Bruno schema-delta migration. Idempotent — safe to re-run any time.

`init_db()` from SQLAlchemy creates missing tables but does NOT add new
columns to existing tables. Whenever we introduce a new column on an
existing table, the addition goes here as an `ALTER TABLE … ADD COLUMN …
IF NOT EXISTS` (via PRAGMA-check + ALTER) entry.

Run after every code deploy:

    python tools/migrate_db.py

Output is one line per delta — green if applied, gray if already present.
Returns exit 0 on success even when no deltas apply.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Make src/ importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.borrower.models import DB_PATH  # noqa: E402


# (table_name, column_name, column_sql)
# Add new rows here as schema additions land. Pattern: keep idempotent.
COLUMN_ADDS = [
    ("loans",          "is_nlv_collateralized", "BOOLEAN NOT NULL DEFAULT 0"),
    ("counterparties", "merit_account_id",      "VARCHAR(64)"),
    # Authorized signatory for generated loan agreements (agreements.py).
    ("counterparties", "represented_by",        "VARCHAR(255)"),
    ("counterparties", "represented_by_title",  "VARCHAR(128)"),
]


def _has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def main(db_path: str = DB_PATH) -> int:
    if not Path(db_path).exists():
        print(f"  ! {db_path} does not exist — run init_db() first.")
        print(f"    .venv/bin/python -c \"from src.borrower.models import init_db; init_db()\"")
        return 0

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    applied = 0
    skipped = 0
    for table, column, col_sql in COLUMN_ADDS:
        if not _table_exists(cur, table):
            print(f"  · skip {table}.{column} — table not in DB yet (init_db will create it)")
            skipped += 1
            continue
        if _has_column(cur, table, column):
            print(f"  · skip {table}.{column} — already present")
            skipped += 1
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_sql}")
        con.commit()
        print(f"  ✓ added {table}.{column}  ({col_sql})")
        applied += 1

    print()
    print(f"summary: {applied} column(s) added, {skipped} already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
