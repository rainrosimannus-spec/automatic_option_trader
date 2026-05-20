"""
Bruno dead-man switch (docs/governance.md §3.2).

If no MesiCap principal logs into the admin dashboard for an extended period,
Bruno assumes loss of operational control and:

  1. Enters WARNING state at WARNING_AFTER_DAYS (default 30): visible banner on
     the admin landing page, alert logged for downstream notification.
  2. Enters FROZEN state at FREEZE_AFTER_DAYS (default 37): all write paths on
     /borrower/* return 423 Locked. Read paths still work.
  3. Designated executor (env DEADMAN_EXECUTOR_NAME / DEADMAN_EXECUTOR_EMAIL)
     receives the offline backup ledger CSV via an out-of-band channel so the
     loan book remains operationally legible to whomever takes custody.

Disabled by default on the dev codebase (Rain's side); enabled on Rasmus's
production clone via env DEADMAN_ENABLED=1. This pattern mirrors the
`bruno_run_integrations` gate used for external integrations.

State is derived live from PrincipalUser.last_login_at; no persistence layer
needed. A "rearm" happens automatically the next time any principal logs in.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.borrower.models import PrincipalUser, get_session_factory


# Defaults — overridable via env at startup
WARNING_AFTER_DAYS = int(os.environ.get("DEADMAN_WARNING_DAYS", "30"))
FREEZE_AFTER_DAYS = int(os.environ.get("DEADMAN_FREEZE_DAYS", "37"))


def is_enabled() -> bool:
    """Dead-man switch is opt-in via env. Off on dev, on in production."""
    return os.environ.get("DEADMAN_ENABLED") == "1"


def executor_contact() -> dict:
    """Read the designated executor contact from env. Returns a dict for the
    template; missing fields render as empty strings."""
    return {
        "name": os.environ.get("DEADMAN_EXECUTOR_NAME", "").strip(),
        "email": os.environ.get("DEADMAN_EXECUTOR_EMAIL", "").strip(),
    }


@dataclass(frozen=True)
class DeadmanState:
    enabled: bool
    state: str                       # 'disabled' | 'normal' | 'warning' | 'frozen'
    last_login_at: Optional[datetime]  # most recent across all principals
    last_login_email: Optional[str]    # which principal had it
    days_since_last_login: Optional[int]
    warning_threshold_days: int = WARNING_AFTER_DAYS
    freeze_threshold_days: int = FREEZE_AFTER_DAYS

    @property
    def is_frozen(self) -> bool:
        return self.state == "frozen"

    @property
    def is_warning(self) -> bool:
        return self.state == "warning"


def compute_state(now: Optional[datetime] = None) -> DeadmanState:
    """Look at PrincipalUser.last_login_at across all unlocked principals and
    classify the current dead-man state. Falls back to created_at when a
    principal has never logged in (covers the cold-start case)."""
    if not is_enabled():
        return DeadmanState(
            enabled=False, state="disabled",
            last_login_at=None, last_login_email=None, days_since_last_login=None,
        )

    now = now or datetime.utcnow()
    session = get_session_factory()()
    try:
        principals = session.query(PrincipalUser).filter(PrincipalUser.locked_at.is_(None)).all()
        if not principals:
            # No principals at all — treat as frozen so a misconfigured deploy
            # doesn't silently let writes through.
            return DeadmanState(
                enabled=True, state="frozen",
                last_login_at=None, last_login_email=None, days_since_last_login=None,
            )

        # For each principal: most recent of last_login_at or created_at
        latest_at = None
        latest_email = None
        for p in principals:
            ts = p.last_login_at or p.created_at
            if ts is None:
                continue
            if latest_at is None or ts > latest_at:
                latest_at = ts
                latest_email = p.email

        if latest_at is None:
            return DeadmanState(
                enabled=True, state="frozen",
                last_login_at=None, last_login_email=None, days_since_last_login=None,
            )

        delta = now - latest_at
        days = delta.days
        if days >= FREEZE_AFTER_DAYS:
            state = "frozen"
        elif days >= WARNING_AFTER_DAYS:
            state = "warning"
        else:
            state = "normal"

        return DeadmanState(
            enabled=True, state=state,
            last_login_at=latest_at,
            last_login_email=latest_email,
            days_since_last_login=days,
        )
    finally:
        session.close()
