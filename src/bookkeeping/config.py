"""
Configuration for the SKXHoldco → Standard Books bridge.

Read from the `bookkeeping:` section of config/settings.yaml (an unmodeled
section, parsed off get_settings().raw — same pattern as `alerts`/`bridge`).
Everything has a safe default, and the bridge is DISABLED + DRY-RUN until you
fill in the connection block and flip the flags, so importing this module can
never affect the live trading app.

Sample settings.yaml block (see README.md for the full walk-through):

    bookkeeping:
      enabled: false           # master switch (keep false until ready)
      dry_run: true            # true = print journals, never POST
      base_currency: EUR       # company accounting currency in Standard Books

      flex:
        token: ""              # SKXHoldco Flex Web Service token
        query_id: ""           # Flex query ID (must include Trades,
                               # CashTransactions, DepositWithdrawal sections)

      standard_books:
        base_url: ""           # http://host:port  (reachable from this host)
        company: "1"           # company/database code in the URL path
        username: ""           # REST user (Full REST-API + write to TRBlock)
        password: ""
        transaction_register: "TRBlock"

      # Chart of accounts — the GL account NUMBERS in your Standard Books.
      # Placeholders below are printed verbatim in dry-run so you can eyeball
      # the structure; replace with real numbers before going live.
      accounts:
        securities:        "TODO_SECURITIES"
        commission:        "TODO_COMMISSION"
        realized_pnl:      "TODO_REALIZED_PNL"
        dividend_income:   "TODO_DIVIDEND_INCOME"
        withholding_tax:   "TODO_WITHHOLDING_TAX"
        interest_income:   "TODO_INTEREST_INCOME"
        interest_expense:  "TODO_INTEREST_EXPENSE"
        fees:              "TODO_FEES"
        fx_gain_loss:      "TODO_FX_GAIN_LOSS"
        equity:            "TODO_EQUITY_INTERCOMPANY"   # deposits/withdrawals
        cash:                                            # brokerage cash by ccy
          USD: "TODO_CASH_USD"
          EUR: "TODO_CASH_EUR"
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


def load_bookkeeping_config() -> BookkeepingConfig:
    """Build a BookkeepingConfig from the `bookkeeping:` settings.yaml section."""
    from src.core.config import get_settings

    raw = (get_settings().raw or {}).get("bookkeeping") or {}

    flex_raw = raw.get("flex") or {}
    sb_raw = raw.get("standard_books") or {}

    cfg = BookkeepingConfig(
        enabled=bool(raw.get("enabled", False)),
        dry_run=bool(raw.get("dry_run", True)),
        base_currency=str(raw.get("base_currency", "EUR")),
        flex=FlexConn(
            token=str(flex_raw.get("token", "")),
            query_id=str(flex_raw.get("query_id", "")),
        ),
        standard_books=SBConn(
            base_url=str(sb_raw.get("base_url", "")).rstrip("/"),
            company=str(sb_raw.get("company", "1")),
            username=str(sb_raw.get("username", "")),
            password=str(sb_raw.get("password", "")),
            transaction_register=str(sb_raw.get("transaction_register", "TRBlock")),
        ),
        accounts=raw.get("accounts") or {},
    )
    return cfg
