"""
Bruno audit log helper.

Every mutation in /borrower routes appends a row to the audit_log table capturing
before/after JSON snapshots of the affected entity, the actor, and basic request
metadata. Caller is responsible for committing the session.
"""
from __future__ import annotations

import enum
import json
from datetime import date, datetime
from typing import Any, Optional

from src.borrower.models import AuditLog


def snapshot(row: Any) -> Optional[dict]:
    """Serialize a SQLAlchemy row's column values to a JSON-safe dict."""
    if row is None:
        return None
    out: dict = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name)
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif isinstance(v, enum.Enum):
            v = v.value
        out[col.name] = v
    return out


def write_audit(
    session,
    *,
    action: str,
    entity_type: str,
    entity_id: Optional[int],
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    actor: str = "rain",
    notes: Optional[str] = None,
    request=None,
) -> None:
    """Append an AuditLog row. Does not commit — caller commits with their own transaction."""
    ip = None
    ua = None
    if request is not None:
        try:
            ip = request.client.host if request.client else None
            ua = request.headers.get("user-agent")
        except Exception:
            pass
    session.add(AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        ip_address=ip,
        user_agent=ua,
        notes=notes,
    ))
