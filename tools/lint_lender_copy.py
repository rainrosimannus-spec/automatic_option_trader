#!/usr/bin/env python3
"""
Banned-terminology lint for lender-facing copy.

Why this exists: using deposit-like terms ("deposit", "savings", "account",
"balance", "fund", "pool", "investment") in materials a lender sees is what
makes a lawyer reclassify a private lending arrangement as a credit
institution or AIF — see src/borrower/LEGAL_CONTEXT.md §1-2 and
docs/governance.md §5.7. This script is a CI guard against accidentally
introducing those words on the lender-facing surface.

Scope: scans only files inside the lender-facing surface, identified by path:
    src/lender_portal/templates/**/*.html   (Phase 3 portal templates)
    src/lender_portal/templates/**/*.txt
    data/statements/templates/**/*.html      (Phase 2 quarterly statement PDF templates)
    data/statements/templates/**/*.txt

The admin-side borrower_*.html templates (where "bank account IBAN" appears
as a legitimate data-entry label) are NOT in scope. Lender-facing emails
should live under src/lender_portal/templates/ too so they're caught.

Usage:
    python3 tools/lint_lender_copy.py             # scan + report + exit 1 on any hit
    python3 tools/lint_lender_copy.py --self-test  # verify the matcher works
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BANNED = [
    "deposit",
    "savings",
    "account",
    "balance",
    "fund",
    "pool",
    "investment",
]

SCAN_GLOBS = [
    "src/lender_portal/templates/**/*.html",
    "src/lender_portal/templates/**/*.txt",
    "data/statements/templates/**/*.html",
    "data/statements/templates/**/*.txt",
]

# Inline escape hatch — a file or line containing this marker is skipped.
# Use sparingly, with a written explanation alongside.
SKIP_MARKER = "lint-lender-copy: allow"


def _scan_text(text: str, path: Path) -> list[tuple[Path, int, str, str]]:
    # Prefix match: catches "deposit", "deposits", "depositing", "pool", "pooled", etc.
    # Lender portal is greenfield, so false positives are cheaper than missed leaks.
    # Any legitimate occurrence can be silenced inline with SKIP_MARKER.
    hits: list[tuple[Path, int, str, str]] = []
    pattern = re.compile(r"\b(" + "|".join(BANNED) + r")\w*", re.IGNORECASE)
    for ln_num, line in enumerate(text.splitlines(), 1):
        if SKIP_MARKER in line:
            continue
        for m in pattern.finditer(line):
            ctx = line.strip()
            if len(ctx) > 120:
                ctx = ctx[:117] + "..."
            hits.append((path, ln_num, m.group(1).lower(), ctx))
    return hits


def _collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in SCAN_GLOBS:
        files.extend(sorted(root.glob(pattern)))
    return files


def main() -> int:
    args = sys.argv[1:]

    if "--self-test" in args:
        sample = (
            "<p>Your loan principal is owed.</p>\n"
            "<p>This is a deposit account with savings interest.</p>\n"
            "<p>Welcome to the fund — your investment is pooled.</p>\n"
        )
        hits = _scan_text(sample, Path("<self-test>"))
        expected = {"deposit", "account", "savings", "fund", "investment", "pool"}
        found = {h[2] for h in hits}
        missing = expected - found
        if missing:
            print(f"SELF-TEST FAILED — missed: {sorted(missing)}", file=sys.stderr)
            return 2
        print(f"SELF-TEST PASSED — matched {sorted(found)} on sample text")
        return 0

    root = Path(__file__).resolve().parent.parent
    files = _collect_files(root)

    if not files:
        print("lint-lender-copy: no lender-facing files in scope yet (Phase 3 portal not built). Lint vacuously clean.")
        return 0

    all_hits: list[tuple[Path, int, str, str]] = []
    for f in files:
        all_hits.extend(_scan_text(f.read_text(), f.relative_to(root)))

    if not all_hits:
        print(f"lint-lender-copy: scanned {len(files)} file(s), no banned terms found.")
        return 0

    print(f"lint-lender-copy: FAILED — {len(all_hits)} banned-term hit(s) across {len({h[0] for h in all_hits})} file(s):")
    for path, ln, term, ctx in all_hits:
        print(f"  {path}:{ln}  [{term}]  {ctx}")
    print()
    print("These words risk regulatory misclassification (LEGAL_CONTEXT.md §1-2 / docs/governance.md §5.7).")
    print("Suggested replacements: loan, credit, principal, facility, outstanding.")
    print(f"Inline escape (use sparingly): add '<!-- {SKIP_MARKER}: reason -->' on the same line.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
