"""
Daily orchestrator: extract → translate → post (or dry-run print). Multi-entity:
runs every configured account (skxholdco, thirona, …) unless --entity narrows it.

    python -m src.bookkeeping.daily_sync                    # dry-run, ALL entities
    python -m src.bookkeeping.daily_sync --entity thirona   # just one
    python -m src.bookkeeping.daily_sync --live             # actually POST (needs creds)
    python -m src.bookkeeping.daily_sync --date 2026-06-23
    python -m src.bookkeeping.daily_sync --entity thirona --xml saved.xml
    python -m src.bookkeeping.daily_sync --entity thirona --describe  # dump TR schema

Idempotency: every journal carries the IBKR external id as its reference. In
LIVE mode a PER-ENTITY ledger (data/bookkeeping_posted_<entity>.jsonl) records
posted refs so a re-run never double-books, and two accounts can't collide.
Dry-run never writes the ledger but reports which refs it would skip.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

from src.bookkeeping.config import (
    BookkeepingConfig,
    load_bookkeeping_config,
    load_bookkeeping_entities,
)
from src.bookkeeping.flex_extract import FlexDay, extract_flex_day, parse_flex
from src.bookkeeping.journal import JournalEntry, translate_day
from src.bookkeeping.standard_books import PostResult, StandardBooksClient
from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class SyncReport:
    dry_run: bool
    entity: str = ""
    total_events: int = 0
    journals: int = 0
    posted: int = 0
    skipped_existing: int = 0
    failed: int = 0
    unmapped_accounts: List[str] = field(default_factory=list)
    results: List[PostResult] = field(default_factory=list)


# ── idempotency ledger (per entity, so two accounts never collide) ──────

def _ledger_path(entity: str) -> Path:
    return Path(f"data/bookkeeping_posted_{entity or 'default'}.jsonl")


def _load_ledger(entity: str) -> Set[str]:
    path = _ledger_path(entity)
    if not path.exists():
        return set()
    refs: Set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            refs.add(json.loads(line)["reference"])
        except Exception:
            continue
    return refs


def _append_ledger(entity: str, result: PostResult) -> None:
    path = _ledger_path(entity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
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

    report = SyncReport(dry_run=effective_dry, entity=cfg.name)
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
    ledger = _load_ledger(cfg.name)

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
            _append_ledger(cfg.name, result)

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
    print(f"  [{cfg.name}] IBKR → Standard Books  |  {mode}")
    print(f"  base currency: {cfg.base_currency}"
          + (f"  |  date filter: {date}" if date else "")
          + f"  |  events: {report.total_events}  journals: {report.journals}")
    if report.unmapped_accounts:
        print(f"  ⚠ unmapped accounts (placeholders shown below): "
              f"{', '.join(report.unmapped_accounts)}")
    print("=" * 78)


def run_all_entities(
    *,
    dry_run: bool | None = None,
    date: str | None = None,
    only: str | None = None,
    print_journals: bool = True,
) -> List[SyncReport]:
    """Run every configured (enabled) entity, or just `only` by name.

    This is the daily driver — the scheduler calls it with no args. Each entity
    is independent: one entity's fetch/translate failure is logged and skipped,
    it never aborts the others.
    """
    reports: List[SyncReport] = []
    for cfg in load_bookkeeping_entities():
        if only and cfg.name != only:
            continue
        if not cfg.enabled and dry_run is None:
            log.info("entity_skipped_disabled", entity=cfg.name)
            continue
        try:
            reports.append(run_daily_sync(
                dry_run=dry_run, date=date, config=cfg, print_journals=print_journals,
            ))
        except Exception as e:
            log.error("entity_sync_failed", entity=cfg.name, error=str(e))
            if print_journals:
                print(f"\n  ✗ [{cfg.name}] sync failed: {e}\n")
    return reports


def _print_footer(report: SyncReport) -> None:
    print("-" * 78)
    print(f"  journals={report.journals}  posted={report.posted}  "
          f"skipped_existing={report.skipped_existing}  failed={report.failed}")
    if report.dry_run:
        print("  DRY-RUN: review the journals above. Re-run with --live once the "
              "chart of accounts + REST credentials are set.")
    print("=" * 78 + "\n")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="IBKR → Standard Books EOD bookkeeping sync")
    ap.add_argument("--live", action="store_true", help="actually POST (default: dry-run)")
    ap.add_argument("--entity", help="run only this entity by name (default: all enabled)")
    ap.add_argument("--date", help="only book events on this date (YYYY-MM-DD)")
    ap.add_argument("--xml", help="path to a saved Flex XML file (skip the Flex service; needs --entity)")
    ap.add_argument("--describe", action="store_true",
                    help="dump existing TRBlock records from the server to reconcile field names")
    args = ap.parse_args(argv)

    if args.describe:
        cfg = load_bookkeeping_config(args.entity)
        cfg.dry_run = False
        client = StandardBooksClient(cfg)
        if not cfg.has_live_credentials:
            print(f"Cannot --describe [{cfg.name}]: standard_books credentials not set")
            return 2
        print(client.describe_register())
        return 0

    dry_run = not args.live

    # --xml replays one saved statement → single entity required.
    if args.xml:
        cfg = load_bookkeeping_config(args.entity)
        if args.live and not cfg.has_live_credentials:
            print(f"Refusing --live [{cfg.name}]: standard_books credentials not set")
            return 2
        run_daily_sync(dry_run=dry_run, date=args.date,
                       flex_xml=Path(args.xml).read_text(), config=cfg)
        return 0

    run_all_entities(dry_run=dry_run, date=args.date, only=args.entity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
