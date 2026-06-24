"""
Configuration for the IBKR → Standard Books bridge (multi-entity).

Read from the `bookkeeping:` section of config/settings.yaml (an unmodeled
section, parsed off get_settings().raw — same pattern as `alerts`/`bridge`).
Everything has a safe default, and the bridge is DISABLED + DRY-RUN until you
fill in the connection blocks and flip the flags, so importing this module can
never affect the live trading app.

Each ENTITY is one IBKR account posting into one Standard Books target. Two are
in play: SKXHoldco (the options login) and Thirona Capital / thenewroma (the
long-term portfolio login, U26413485). An entity can INHERIT its Flex creds from
the trading app via `flex_source` (ibkr | portfolio) instead of repeating them.

Sample settings.yaml block (see README.md for the full walk-through):

    bookkeeping:
      enabled: false           # master switch (keep false until ready)
      dry_run: true            # true = print journals, never POST
      entities:
        - name: skxholdco
          flex_source: ibkr    # reuse ibkr.flex_token / ibkr.flex_query_id
          base_currency: EUR
          standard_books:
            base_url: ""        # http://host:port  (reachable from this host)
            company: "1"        # company/database code in the URL path
            username: ""        # REST user (Full REST-API + write to TRBlock)
            password: ""
            transaction_register: "TRBlock"
          accounts:             # the GL account NUMBERS in this company's books
            securities: "TODO_SECURITIES"
            commission: "TODO_COMMISSION"
            realized_pnl: "TODO_REALIZED_PNL"
            dividend_income: "TODO_DIVIDEND_INCOME"
            withholding_tax: "TODO_WITHHOLDING_TAX"
            interest_income: "TODO_INTEREST_INCOME"
            interest_expense: "TODO_INTEREST_EXPENSE"
            fees: "TODO_FEES"
            fx_gain_loss: "TODO_FX_GAIN_LOSS"
            equity: "TODO_EQUITY_INTERCOMPANY"   # deposits/withdrawals
            cash: { USD: "TODO_CASH_USD", EUR: "TODO_CASH_EUR" }

        - name: thirona
          flex_source: portfolio   # reuse portfolio.flex_token / flex_query_id
          base_currency: EUR
          standard_books:          # Thirona's own Standard Books company (may be
            base_url: ""           # the same server, different `company`, or a
            company: "2"           # wholly separate install — your call)
            username: ""
            password: ""
          accounts:
            # … Thirona's own GL numbers (same logical keys as above) …

An entity may instead inherit Flex creds by leaving `flex_source` unset and
giving its own `flex: {token, query_id}` block. The Flex query MUST include the
Trades, Cash Transactions, and Deposits & Withdrawals sections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from src.core.logger import get_logger

log = get_logger(__name__)

# Logical account keys the translator references. Kept here so a missing/typo'd
# account in settings.yaml fails loudly at load time, not mid-posting.
REQUIRED_ACCOUNT_KEYS = (
    "securities",
    "commission",
    "realized_pnl",
    "dividend_income",
    "withholding_tax",
    "interest_income",
    "interest_expense",
    "fees",
    "fx_gain_loss",
    "equity",
)

# Printed (and used as a sentinel) when an account isn't configured yet.
_MISSING = "UNMAPPED"


@dataclass
class FlexConn:
    token: str = ""
    query_id: str = ""


@dataclass
class SBConn:
    base_url: str = ""
    company: str = "1"
    username: str = ""
    password: str = ""
    transaction_register: str = "TRBlock"


@dataclass
class BookkeepingConfig:
    """One bookkeeping ENTITY (a single IBKR account → one Standard Books target).

    The bridge is multi-entity: SKXHoldco (the options login) and Thirona Capital
    / thenewroma (the long-term portfolio login, U26413485) are each one of these,
    with their own Flex creds, Standard Books company, base currency and chart of
    accounts. The journal translator is account-agnostic, so the only per-entity
    differences live here.
    """
    name: str = "default"
    # When set ("ibkr" | "portfolio"), Flex creds are inherited from that
    # settings.yaml section instead of this entity's own `flex` block — so the
    # bridge reuses the credentials the trading app already holds.
    flex_source: str = ""
    enabled: bool = False
    dry_run: bool = True
    base_currency: str = "EUR"
    flex: FlexConn = field(default_factory=FlexConn)
    standard_books: SBConn = field(default_factory=SBConn)
    accounts: Dict[str, object] = field(default_factory=dict)

    # ── account lookup helpers ──────────────────────────────────────────
    def account(self, key: str) -> str:
        """GL account number for a logical key, or the UNMAPPED sentinel."""
        val = self.accounts.get(key)
        return str(val) if val else _MISSING

    def cash_account(self, currency: str) -> str:
        """Brokerage-cash GL account for a currency (falls back to base ccy)."""
        cash = self.accounts.get("cash") or {}
        if isinstance(cash, dict):
            return str(
                cash.get(currency)
                or cash.get(self.base_currency)
                or _MISSING
            )
        return _MISSING

    @property
    def has_live_credentials(self) -> bool:
        sb = self.standard_books
        return bool(sb.base_url and sb.username and sb.password)

    def unmapped_accounts(self) -> list[str]:
        """Logical keys still pointing at a placeholder/missing account."""
        missing = [k for k in REQUIRED_ACCOUNT_KEYS
                   if self.account(k) in (_MISSING,) or self.account(k).startswith("TODO_")]
        # cash is a sub-map; flag if neither base ccy nor any entry is real
        cash = self.accounts.get("cash") or {}
        if not isinstance(cash, dict) or not any(
            v and not str(v).startswith("TODO_") for v in cash.values()
        ):
            missing.append("cash")
        return missing


def _resolve_flex(entity_raw: dict, flex_source: str) -> FlexConn:
    """Flex creds for an entity: either inherited from a settings.yaml section
    (`flex_source: ibkr|portfolio`) or read from the entity's own `flex` block."""
    if flex_source:
        from src.core.config import get_settings
        s = get_settings()
        section = getattr(s, flex_source, None)
        if section is not None:
            return FlexConn(
                token=str(getattr(section, "flex_token", "") or ""),
                query_id=str(getattr(section, "flex_query_id", "") or ""),
            )
    flex_raw = entity_raw.get("flex") or {}
    return FlexConn(
        token=str(flex_raw.get("token", "")),
        query_id=str(flex_raw.get("query_id", "")),
    )


def _build_entity(entity_raw: dict, *, enabled: bool, dry_run: bool) -> BookkeepingConfig:
    sb_raw = entity_raw.get("standard_books") or {}
    flex_source = str(entity_raw.get("flex_source", ""))
    return BookkeepingConfig(
        name=str(entity_raw.get("name", "default")),
        flex_source=flex_source,
        enabled=enabled,
        dry_run=dry_run,
        base_currency=str(entity_raw.get("base_currency", "EUR")),
        flex=_resolve_flex(entity_raw, flex_source),
        standard_books=SBConn(
            base_url=str(sb_raw.get("base_url", "")).rstrip("/"),
            company=str(sb_raw.get("company", "1")),
            username=str(sb_raw.get("username", "")),
            password=str(sb_raw.get("password", "")),
            transaction_register=str(sb_raw.get("transaction_register", "TRBlock")),
        ),
        accounts=entity_raw.get("accounts") or {},
    )


def load_bookkeeping_entities() -> list[BookkeepingConfig]:
    """All configured bookkeeping entities from the `bookkeeping:` settings block.

    Supports both shapes:
      • multi-entity:  bookkeeping.entities: [ {name: skxholdco, ...}, {name: thirona, ...} ]
      • single legacy: a flat bookkeeping block (treated as one unnamed entity)
    The top-level `enabled`/`dry_run` apply to every entity.
    """
    from src.core.config import get_settings

    raw = (get_settings().raw or {}).get("bookkeeping") or {}
    enabled = bool(raw.get("enabled", False))
    dry_run = bool(raw.get("dry_run", True))

    entities_raw = raw.get("entities")
    if entities_raw:
        return [_build_entity(e, enabled=enabled, dry_run=dry_run) for e in entities_raw]
    # legacy single-block form
    return [_build_entity(raw, enabled=enabled, dry_run=dry_run)]


def load_bookkeeping_config(name: str | None = None) -> BookkeepingConfig:
    """Single entity by name (or the first configured) — convenience wrapper."""
    entities = load_bookkeeping_entities()
    if name:
        for e in entities:
            if e.name == name:
                return e
        raise KeyError(f"no bookkeeping entity named {name!r}")
    return entities[0]
