"""
LHV CAMT.053 statement file ingestion.

CAMT.053 is the ISO 20022 bank statement format. LHV exports daily statements
in this XML format; this module parses one file and lands each entry as a row
in the `bank_transactions` staging table.

See docs/governance.md §4.1 for the design choice (CAMT.053 file ingestion
first, PSD2 live API later) and the matching workflow.

This file ingestion is safe to run on any environment — it touches only Bruno's
own DB, not LHV. (The PSD2 *API* integration would be gated to Rasmus's clone
via `bruno_run_integrations`; that lives in a separate module if/when built.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree as ET

from src.borrower.lhv_accounts import is_known
from src.borrower.models import BankTransaction, LoanMovement, MovementType


# CAMT.053 lives in iso:20022 namespace; the version suffix varies by ISO release.
# We strip namespaces manually for robustness across versions.

def _strip_ns(tag: str) -> str:
    """Return the local-name part of a namespaced tag."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _findtext(node, *path) -> Optional[str]:
    """Walk a path of local names, return text of the leaf or None."""
    cur = node
    for name in path:
        nxt = None
        for child in cur:
            if _strip_ns(child.tag) == name:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return (cur.text or "").strip() or None


def _find_all(node, name) -> list:
    return [c for c in node if _strip_ns(c.tag) == name]


@dataclass
class ParsedEntry:
    statement_id: Optional[str]
    entry_ref: Optional[str]
    value_date: date
    booking_date: Optional[date]
    amount: float                  # signed: + credit, - debit
    currency: str
    account_iban: str
    counterparty_iban: Optional[str]
    counterparty_name: Optional[str]
    reference_text: Optional[str]


def parse_camt053(xml_content: str) -> List[ParsedEntry]:
    """Parse a CAMT.053 statement file into a flat list of ParsedEntry."""
    root = ET.fromstring(xml_content)
    out: List[ParsedEntry] = []

    # Walk to <Stmt> nodes (one per statement, usually one per file)
    for stmt in root.iter():
        if _strip_ns(stmt.tag) != "Stmt":
            continue

        stmt_id = _findtext(stmt, "Id")
        acct_iban = _findtext(stmt, "Acct", "Id", "IBAN")

        for ntry in _find_all(stmt, "Ntry"):
            amt_raw = _findtext(ntry, "Amt")
            ccy = None
            # Currency is on the Amt element as @Ccy
            for child in ntry:
                if _strip_ns(child.tag) == "Amt":
                    ccy = child.attrib.get("Ccy")
                    break
            if amt_raw is None or ccy is None:
                continue

            amount = float(amt_raw)
            ind = _findtext(ntry, "CdtDbtInd")  # "CRDT" or "DBIT"
            if ind == "DBIT":
                amount = -amount

            bdt = _findtext(ntry, "BookgDt", "Dt")
            vdt = _findtext(ntry, "ValDt", "Dt")
            booking_date = datetime.fromisoformat(bdt).date() if bdt else None
            value_date = datetime.fromisoformat(vdt).date() if vdt else booking_date

            entry_ref = _findtext(ntry, "AcctSvcrRef") or _findtext(ntry, "NtryRef")

            # Drill into TxDtls (one per Ntry typically) for counterparty + remittance info
            counterparty_iban = None
            counterparty_name = None
            reference_text = None
            for tx_dtls in ntry.iter():
                if _strip_ns(tx_dtls.tag) != "TxDtls":
                    continue
                # Counterparty side depends on direction: DBIT → look at Cdtr, CRDT → look at Dbtr
                tag = "Dbtr" if ind == "CRDT" else "Cdtr"
                acct_tag = "DbtrAcct" if ind == "CRDT" else "CdtrAcct"
                counterparty_name = _findtext(tx_dtls, "RltdPties", tag, "Nm") or counterparty_name
                counterparty_iban = _findtext(tx_dtls, "RltdPties", acct_tag, "Id", "IBAN") or counterparty_iban
                # Remittance info: Ustrd = unstructured free text (most common)
                rmt = _findtext(tx_dtls, "RmtInf", "Ustrd")
                if rmt and not reference_text:
                    reference_text = rmt

            if value_date is None:
                continue  # malformed; skip

            out.append(ParsedEntry(
                statement_id=stmt_id,
                entry_ref=entry_ref,
                value_date=value_date,
                booking_date=booking_date,
                amount=amount,
                currency=ccy,
                account_iban=acct_iban or "",
                counterparty_iban=counterparty_iban,
                counterparty_name=counterparty_name,
                reference_text=reference_text,
            ))
    return out


def ingest_entries(session, entries: List[ParsedEntry], source_file: str) -> dict:
    """
    Insert ParsedEntry rows into bank_transactions, then attempt auto-match
    against existing LoanMovement rows by (bank_reference == entry_ref OR
    bank_reference == reference_text) AND amount magnitude AND value_date.

    Returns: {'ingested': n, 'duplicates': n, 'auto_matched': n, 'rejected_unknown_iban': n}
    """
    result = {"ingested": 0, "duplicates": 0, "auto_matched": 0, "rejected_unknown_iban": 0}

    for e in entries:
        if not is_known(e.account_iban):
            # Skip entries on unknown accounts — only ingest for our registered IBANs.
            result["rejected_unknown_iban"] += 1
            continue

        # Idempotency on (source_file, entry_ref) if entry_ref is set,
        # else on (source_file, value_date, amount, counterparty_iban).
        q = session.query(BankTransaction).filter(BankTransaction.source_file == source_file)
        if e.entry_ref:
            existing = q.filter(BankTransaction.entry_ref == e.entry_ref).first()
        else:
            existing = q.filter(
                BankTransaction.value_date == e.value_date,
                BankTransaction.amount == e.amount,
                BankTransaction.counterparty_iban == e.counterparty_iban,
            ).first()
        if existing is not None:
            result["duplicates"] += 1
            continue

        bt = BankTransaction(
            source="camt053",
            source_file=source_file,
            statement_id=e.statement_id,
            entry_ref=e.entry_ref,
            value_date=e.value_date,
            booking_date=e.booking_date,
            amount=e.amount,
            currency=e.currency,
            account_iban=e.account_iban,
            counterparty_iban=e.counterparty_iban,
            counterparty_name=e.counterparty_name,
            reference_text=e.reference_text,
            status="unmatched",
        )

        # Try auto-match: a LoanMovement whose bank_reference equals either the
        # CAMT entry_ref or the reference_text, and whose amount magnitude matches
        # and value_date is within ±2 days.
        candidates = []
        for ref_candidate in filter(None, [e.entry_ref, e.reference_text]):
            q = session.query(LoanMovement).filter(LoanMovement.bank_reference == ref_candidate)
            candidates.extend(q.all())
        # De-dupe + filter by amount and date proximity
        seen = set()
        for m in candidates:
            if m.id in seen:
                continue
            seen.add(m.id)
            if abs(m.amount - abs(e.amount)) > 0.01:
                continue
            day_delta = abs((m.movement_date - e.value_date).days) if m.movement_date else 999
            if day_delta > 2:
                continue
            bt.matched_movement_id = m.id
            bt.status = "matched"
            result["auto_matched"] += 1
            break

        session.add(bt)
        result["ingested"] += 1

    session.commit()
    return result


def ingest_camt053_file(session, path: str | Path) -> dict:
    """Convenience: read a CAMT.053 file from disk and ingest it."""
    p = Path(path)
    entries = parse_camt053(p.read_text(encoding="utf-8"))
    return ingest_entries(session, entries, source_file=p.name)
