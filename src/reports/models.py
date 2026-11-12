"""
Flex archive tables — the durable record behind account statement PDFs.

Everything here lives in the MAIN database (data/trades.db). The options DB
holds only `positions` and `trades` (see src/core/database.py binds), and the
archive deliberately spans both accounts in one set of tables discriminated by
`side` + `account_id` — a statement never joins against the live ledger.

Idempotency is the whole point of these tables: the same Flex statement can be
imported any number of times (daily job, manual refetch, re-uploaded XML) and
must converge on the same rows.

    flex_statement_files   one row per imported statement (raw XML kept on disk)
    flex_events            transactions — trades, cash, deposits, transfers, CAs
    flex_position_snapshots open positions as reported ON a given date
    flex_equity_summaries  account totals (cash/stock/options/NAV) on a date
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, String, Text, UniqueConstraint,
)

from src.core.models import Base


class FlexStatementFile(Base):
    """An imported Flex statement. The raw XML stays on disk under
    data/flex_archive/{side}/ so the archive can always be rebuilt from source."""
    __tablename__ = "flex_statement_files"

    id = Column(Integer, primary_key=True)
    side = Column(String(12), nullable=False, index=True)      # options | portfolio
    account_id = Column(String(20), nullable=False, index=True)
    from_date = Column(String(10), nullable=False)             # YYYY-MM-DD
    to_date = Column(String(10), nullable=False)               # YYYY-MM-DD
    period = Column(String(40), default="")                    # IBKR's own label
    when_generated = Column(String(20), default="")
    query_name = Column(String(80), default="")
    source = Column(String(12), default="api")                 # api | upload
    filename = Column(String(255), default="")                 # original upload name
    xml_path = Column(String(500), default="")
    sha256 = Column(String(64), nullable=False, index=True)
    n_events = Column(Integer, default=0)
    n_positions = Column(Integer, default=0)
    imported_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("side", "account_id", "sha256", name="uq_flex_file_account_sha"),
    )


class FlexEvent(Base):
    """One account transaction, in its original currency.

    `kind` buckets the IBKR element it came from:
        trade | cash | deposit | transfer | corporate_action

    `event_type` is the finer label used on the statement — for trades the
    BUY/SELL side, for cash IBKR's own type string ("Dividends", "Withholding
    Tax", "Broker Interest Paid", ...).

    Amounts are NEVER converted at storage time (Bruno's rule, and it's the
    right one for tax): `amount` is in `currency`, and `fx_rate_to_base` is
    IBKR's own rate to the account's base currency at the time.
    """
    __tablename__ = "flex_events"

    id = Column(Integer, primary_key=True)
    side = Column(String(12), nullable=False, index=True)
    account_id = Column(String(20), nullable=False, index=True)
    kind = Column(String(20), nullable=False, index=True)
    external_id = Column(String(80), nullable=False)           # transactionID / tradeID
    event_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    settle_date = Column(String(10), default="")
    event_type = Column(String(60), default="")
    symbol = Column(String(40), default="")
    description = Column(String(255), default="")
    asset_category = Column(String(10), default="")            # STK OPT FOP CASH ...
    put_call = Column(String(4), default="")
    strike = Column(Float)
    expiry = Column(String(10), default="")
    quantity = Column(Float, default=0.0)
    price = Column(Float, default=0.0)
    proceeds = Column(Float, default=0.0)                      # signed, excl. commission
    commission = Column(Float, default=0.0)                    # signed, normally <= 0
    tax = Column(Float, default=0.0)
    amount = Column(Float, default=0.0)                        # the cash effect, signed
    cost_basis = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    currency = Column(String(5), default="USD")
    fx_rate_to_base = Column(Float, default=1.0)
    level_of_detail = Column(String(20), default="")
    statement_id = Column(Integer, index=True)
    raw_json = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    # `side` is part of the natural key as well as `account_id`: the two sides
    # are separate companies' books and must never merge into each other, even
    # if they were ever pointed at the same IBKR account.
    __table_args__ = (
        UniqueConstraint("side", "account_id", "kind", "external_id", name="uq_flex_event"),
        Index("ix_flex_event_range", "side", "event_date"),
    )


class FlexPositionSnapshot(Base):
    """An open position exactly as IBKR reported it on `report_date`.

    This is what makes "holdings on 31.12." answerable — the internal DBs hold
    only current state, so an as-of view has to come from the broker's own
    period-end report.
    """
    __tablename__ = "flex_position_snapshots"

    id = Column(Integer, primary_key=True)
    side = Column(String(12), nullable=False, index=True)
    account_id = Column(String(20), nullable=False, index=True)
    report_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    symbol = Column(String(40), default="")
    description = Column(String(255), default="")
    asset_category = Column(String(10), default="")
    put_call = Column(String(4), default="")
    strike = Column(Float)
    expiry = Column(String(10), default="")
    con_id = Column(String(20), default="")
    quantity = Column(Float, default=0.0)
    multiplier = Column(Float, default=1.0)
    mark_price = Column(Float, default=0.0)
    position_value = Column(Float, default=0.0)
    cost_basis_price = Column(Float, default=0.0)
    cost_basis_money = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    currency = Column(String(5), default="USD")
    fx_rate_to_base = Column(Float, default=1.0)
    level_of_detail = Column(String(20), default="")
    statement_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_flex_pos_asof", "side", "report_date"),
    )


class FlexEquitySummary(Base):
    """Account totals in base currency on a report date — the header line above
    the holdings table (<EquitySummaryByReportDateInBase> in the Flex XML)."""
    __tablename__ = "flex_equity_summaries"

    id = Column(Integer, primary_key=True)
    side = Column(String(12), nullable=False, index=True)
    account_id = Column(String(20), nullable=False, index=True)
    report_date = Column(String(10), nullable=False, index=True)
    currency = Column(String(5), default="")                   # base currency
    cash = Column(Float, default=0.0)
    stock = Column(Float, default=0.0)
    options = Column(Float, default=0.0)
    dividend_accruals = Column(Float, default=0.0)
    interest_accruals = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    statement_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("side", "account_id", "report_date", name="uq_flex_equity_asof"),
    )
