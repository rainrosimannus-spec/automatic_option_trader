"""
Daily orchestrator: extract → translate → post (or dry-run print).

    python -m src.bookkeeping.daily_sync            # dry-run (default)
    python -m src.bookkeeping.daily_sync --live     # actually POST (needs creds)
    python -m src.bookkeeping.daily_sync --date 2026-06-23
    python -m src.bookkeeping.daily_sync --describe  # dump TR schema from server

Idempotency: every journal carries the IBKR external id as its reference. In
LIVE mode a local ledger (data/bookkeeping_posted.jsonl) records posted refs so
a re-run never double-books. Dry-run never writes the ledger (so you always see
the full day) but does report which refs the ledger would skip.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

from src.bookkeeping.config import BookkeepingConfig, load_bookkeeping_config
from src.bookkeeping.flex_extract import FlexDay, extract_flex_day, parse_flex
from src.bookkeeping.journal import JournalEntry, translate_day
from src.bookkeeping.standard_books import PostResult, StandardBooksClient
from src.core.logger import get_logger

log = get_logger(__name__)

_LEDGER_PATH = Path("data/bookkeeping_posted.jsonl")


@dataclass
class SyncReport:
    dry_run: bool
    total_events: int = 0
    journals: int = 0
    posted: int = 0
    skipped_existing: int = 0
    failed: int = 0
    unmapped_accounts: List[str] = field(default_factory=list)
    results: List[PostResult] = field(default_factory=list)


# ── idempotency ledger ─────────────────────────────────────────────────

def _load_ledger() -> Set[str]:
    if not _LEDGER_PATH.exists():
        return set()
    refs: Set[str] = set()
    for line in _LEDGER_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            refs.add(json.loads(line)["reference"])
        except Exception:
            continue
    return refs


def _append_ledger(result: PostResult) -> None:
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LEDGER_PATH.open("a") as f:
        f.write(json.dumps({
            "reference": result.reference,
            "url": result.url,
            "sequence": result.sequence,
        }) + "\n")


# ── run ─────────────────────────────────────────────────────────────────

def run_daily_sync(
    *,
    dry_run: bool | None = None,
    date: str | None = None,
    flex_xml: str | None = None,
    config: BookkeepingConfig | None = None,
    print_journals: bool = True,
) -> SyncReport:
    """Run one EOD sync.

    dry_run    None → use config.dry_run; True/False overrides it.
    date       'YYYY-MM-DD' → only book events on that date (default: all in the
               Flex window).
    flex_xml   feed a raw XML string instead of hitting the Flex service (tests
               / replaying a saved statement).
    """
    cfg = config or load_bookkeeping_config()
    effective_dry = cfg.dry_run if dry_run is None else dry_run
    cfg.dry_run = effective_dry  # client reads cfg.dry_run

    report = SyncReport(dry_run=effective_dry)
    report.unmapped_accounts = cfg.unmapped_accounts()

    # 1. extract
    if flex_xml is not None:
        day: FlexDay = parse_flex(flex_xml)
    else:
        day = extract_flex_day(cfg.flex.token, cfg.flex.query_id)
    report.total_events = day.total_events

    # 2. translate
    entries: List[JournalEntry] = translate_day(day, cfg)
    if date:
        entries = [e for e in entries if e.date == date]
    report.journals = len(entries)

    # 3. post / dry-run
    client = StandardBooksClient(cfg)
    ledger = _load_ledger()

    if print_journals:
        _print_header(cfg, report, date)

    for je in entries:
        if je.reference in ledger:
            report.skipped_existing += 1
            if print_journals:
                print(f"  · skip (already posted) {je.reference}  {je.comment}")
            continue

        if print_journals:
            print(client.render_dry_run(je))

        result = client.post_journal(je)
        report.results.append(result)
        if not result.ok:
            report.failed += 1
        elif result.dry_run:
            pass  # nothing committed
        else:
            report.posted += 1
            _append_ledger(result)

    if print_journals:
        _print_footer(report)
    log.info(
        "bookkeeping_sync_done",
        dry_run=report.dry_run, journals=report.journals,
        posted=report.posted, skipped=report.skipped_existing, failed=report.failed,
    )
    return report


def _print_header(cfg: BookkeepingConfig, report: SyncReport, date: str | None) -> None:
    mode = "DRY-RUN (nothing sent)" if report.dry_run else "LIVE → POSTing to Standard Books"
    print("\n" + "=" * 78)
    print(f"  SKXHoldco → Standard Books  |  {mode}")
    print(f"  base currency: {cfg.base_currency}"
          + (f"  |  date filter: {date}" if date else "")
          + f"  |  events: {report.total_events}  journals: {report.journals}")
    if report.unmapped_accounts:
        print(f"  ⚠ unmapped accounts (placeholders shown below): "
              f"{', '.join(report.unmapped_accounts)}")
    print("=" * 78)


def _print_footer(report: SyncReport) -> None:
    print("-" * 78)
    print(f"  journals={report.journals}  posted={report.posted}  "
          f"skipped_existing={report.skipped_existing}  failed={report.failed}")
    if report.dry_run:
        print("  DRY-RUN: review the journals above. Re-run with --live once the "
              "chart of accounts + REST credentials are set.")
    print("=" * 78 + "\n")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SKXHoldco → Standard Books EOD bookkeeping sync")
    ap.add_argument("--live", action="store_true", help="actually POST (default: dry-run)")
    ap.add_argument("--date", help="only book events on this date (YYYY-MM-DD)")
    ap.add_argument("--xml", help="path to a saved Flex XML file (skip the Flex service)")
    ap.add_argument("--describe", action="store_true",
                    help="dump existing TRBlock records from the server to reconcile field names")
    args = ap.parse_args(argv)

    cfg = load_bookkeeping_config()

    if args.describe:
        cfg.dry_run = False
        client = StandardBooksClient(cfg)
        if not cfg.has_live_credentials:
            print("Cannot --describe: standard_books credentials not set in settings.yaml")
            return 2
        print(client.describe_register())
        return 0

    flex_xml = Path(args.xml).read_text() if args.xml else None
    dry_run = not args.live
    if args.live and not cfg.has_live_credentials:
        print("Refusing --live: standard_books credentials not set in settings.yaml")
        return 2

    run_daily_sync(dry_run=dry_run, date=args.date, flex_xml=flex_xml)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
