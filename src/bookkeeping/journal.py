"""
Double-entry translation: IBKR Flex events → balanced journal entries.

Booking conventions
-------------------
• Cost basis only (no mark-to-market revaluation of open positions).
• Every entry is posted in the company BASE currency. IBKR amounts are converted
  with the row's fxRateToBase; the original-currency figure is kept in each row's
  narrative for traceability.
• Each JournalEntry is asserted balanced (Σdebit == Σcredit) before it leaves
  the translator — an unbalanced entry is a bug, not something to post.

The realized P&L on a sell is booked GROSS (proceeds − cost basis) with the
commission shown on its own line, so the P&L line matches the trade's economic
gain and the commission lands in its own expense account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.bookkeeping.config import BookkeepingConfig
from src.bookkeeping.flex_extract import (
    CashEvent,
    DepositEvent,
    FlexDay,
    TradeEvent,
)
from src.core.logger import get_logger

log = get_logger(__name__)

# Sub-cent residue from FX conversion is rounded onto the largest line.
_BALANCE_TOL = 0.01


@dataclass
class JournalRow:
    account: str
    debit: float = 0.0
    credit: float = 0.0
    text: str = ""


@dataclass
class JournalEntry:
    date: str
    reference: str            # idempotency key (IBKR external id) → TR ref field
    comment: str              # human narrative for the transaction header
    currency: str             # base currency the rows are expressed in
    rows: List[JournalRow] = field(default_factory=list)
    source_kind: str = ""     # trade / dividend / interest / fee / fx / deposit

    # ── builder helpers (amounts already in base currency) ──────────────
    def debit(self, account: str, amount: float, text: str = "") -> None:
        self._add(account, amount, is_debit=True, text=text)

    def credit(self, account: str, amount: float, text: str = "") -> None:
        self._add(account, amount, is_debit=False, text=text)

    def signed(self, account: str, amount: float, text: str = "") -> None:
        """Positive → debit, negative → credit (flips the side)."""
        if amount >= 0:
            self.debit(account, amount, text)
        else:
            self.credit(account, -amount, text)

    def _add(self, account: str, amount: float, is_debit: bool, text: str) -> None:
        amount = round(float(amount), 2)
        if amount == 0:
            return
        # A negative debit is really a credit (and vice-versa) — normalize.
        if amount < 0:
            is_debit = not is_debit
            amount = -amount
        self.rows.append(JournalRow(
            account=account,
            debit=amount if is_debit else 0.0,
            credit=0.0 if is_debit else amount,
            text=text,
        ))

    # ── balance ─────────────────────────────────────────────────────────
    @property
    def total_debit(self) -> float:
        return round(sum(r.debit for r in self.rows), 2)

    @property
    def total_credit(self) -> float:
        return round(sum(r.credit for r in self.rows), 2)

    @property
    def imbalance(self) -> float:
        return round(self.total_debit - self.total_credit, 2)

    def is_balanced(self) -> bool:
        return abs(self.imbalance) <= _BALANCE_TOL

    def finalize(self) -> "JournalEntry":
        """Round residual FX cents onto the largest row, then assert balance."""
        diff = self.imbalance
        if 0 < abs(diff) <= _BALANCE_TOL and self.rows:
            biggest = max(self.rows, key=lambda r: max(r.debit, r.credit))
            if diff > 0:               # debits exceed credits → add to a credit
                biggest.credit = round(biggest.credit + diff, 2)
            else:
                biggest.debit = round(biggest.debit - diff, 2)
        if not self.is_balanced():
            raise ValueError(
                f"Unbalanced journal {self.reference!r} ({self.source_kind}): "
                f"debit={self.total_debit} credit={self.total_credit}"
            )
        return self


def _to_base(amount: float, fx_rate_to_base: float) -> float:
    return round(amount * (fx_rate_to_base or 1.0), 2)


def _ccy_note(amount: float, currency: str, base: str, fx: float) -> str:
    if currency == base or (fx or 1.0) == 1.0:
        return ""
    return f" [{amount:+,.2f} {currency} @ {fx:.4f}]"


# ── per-event translators ──────────────────────────────────────────────

def translate_trade(t: TradeEvent, cfg: BookkeepingConfig) -> JournalEntry | None:
    base = cfg.base_currency
    if t.is_fx:
        return _translate_fx(t, cfg)

    je = JournalEntry(
        date=t.date,
        reference=f"IBKR:{t.external_id}",
        comment=f"{t.buy_sell} {abs(t.quantity):g} {t.symbol} ({t.asset_category})",
        currency=base,
        source_kind="trade",
    )
    sec = cfg.account("securities")
    cash = cfg.cash_account(t.currency)
    comm = cfg.account("commission")
    pnl = cfg.account("realized_pnl")

    proceeds_b = _to_base(t.proceeds, t.fx_rate_to_base)        # buy<0, sell>0
    commission_b = _to_base(t.commission, t.fx_rate_to_base)    # normally <=0
    net_cash_b = _to_base(t.net_cash, t.fx_rate_to_base)
    note = _ccy_note(t.proceeds, t.currency, base, t.fx_rate_to_base)

    if t.buy_sell == "BUY":
        # Securities (cost, excl. commission) Dr; commission Dr; cash Cr.
        je.debit(sec, abs(proceeds_b), f"{t.symbol} cost{note}")
        je.debit(comm, abs(commission_b), f"{t.symbol} commission")
        je.credit(cash, abs(net_cash_b), f"cash {t.currency}")
    else:  # SELL
        cost_basis_b = _to_base(t.cost_basis, t.fx_rate_to_base)
        # Gross realized P&L = proceeds − cost basis (commission booked separately).
        gross_pnl_b = round(abs(proceeds_b) - cost_basis_b, 2)
        je.debit(cash, abs(net_cash_b), f"cash {t.currency}")
        je.debit(comm, abs(commission_b), f"{t.symbol} commission")
        je.credit(sec, cost_basis_b, f"{t.symbol} cost basis")
        # gain (gross_pnl>0) is income → credit; loss → debit. signed() maps
        # positive→debit, so negate to put a gain on the credit side.
        je.signed(pnl, -gross_pnl_b, f"{t.symbol} realized P&L{note}")

    return je.finalize()


def _translate_fx(t: TradeEvent, cfg: BookkeepingConfig) -> JournalEntry | None:
    """FX conversion (Trade assetCategory=CASH, e.g. EUR.USD).

    Simplified: move base-converted cash between the two currency cash accounts
    and plug any residue to FX gain/loss. NOTE: validate against real SKXHoldco
    FX rows before going live — IBKR's CASH-trade representation varies by how
    the conversion was booked.
    """
    base = cfg.base_currency
    # symbol like "EUR.USD" → base/quote of the pair
    pair = (t.symbol or "").replace(" ", "")
    quote_ccy = pair.split(".")[-1] if "." in pair else t.currency
    deal_ccy = pair.split(".")[0] if "." in pair else base

    je = JournalEntry(
        date=t.date,
        reference=f"IBKR:{t.external_id}",
        comment=f"FX {t.symbol} {t.buy_sell} {abs(t.quantity):g}",
        currency=base,
        source_kind="fx",
    )
    # The trade's fxRateToBase converts the QUOTE currency (t.currency). A leg
    # already in the base currency must NOT be reconverted (rate 1.0); a leg in
    # neither base nor quote ccy can't be valued from this row alone → it falls
    # to the FX plug below (flagged for validation).
    def leg_base(amount: float, ccy: str) -> float:
        if ccy == base:
            return round(amount, 2)
        if ccy == t.currency:
            return _to_base(amount, t.fx_rate_to_base)
        return _to_base(amount, t.fx_rate_to_base)

    deal_amt_b = leg_base(t.quantity, deal_ccy)
    proceeds_b = leg_base(t.proceeds, quote_ccy)
    # Receiving deal_ccy (debit its cash), paying quote_ccy (credit its cash).
    je.signed(cfg.cash_account(deal_ccy), deal_amt_b, f"{deal_ccy} leg")
    je.signed(cfg.cash_account(quote_ccy), proceeds_b, f"{quote_ccy} leg")
    # plug the rest to FX gain/loss so the entry balances
    residue = -je.imbalance
    if abs(residue) > _BALANCE_TOL:
        je.signed(cfg.account("fx_gain_loss"), residue, "FX gain/loss plug")
    return je.finalize()


# CashTransaction "type" → handler. Matched case-insensitively on a substring.
def translate_cash(c: CashEvent, cfg: BookkeepingConfig) -> JournalEntry | None:
    base = cfg.base_currency
    amount_b = _to_base(c.amount, c.fx_rate_to_base)
    if amount_b == 0:
        return None

    cash = cfg.cash_account(c.currency)
    note = _ccy_note(c.amount, c.currency, base, c.fx_rate_to_base)
    typ = (c.type or "").lower()
    sym = (c.symbol + " ") if c.symbol else ""

    je = JournalEntry(
        date=c.date,
        reference=f"IBKR:{c.external_id}",
        comment=f"{c.type}: {sym}{c.description}".strip()[:200],
        currency=base,
        source_kind="cash",
    )

    if "withholding" in typ:
        # tax withheld on a dividend — amount is negative (cash out)
        je.debit(cfg.account("withholding_tax"), abs(amount_b), f"{sym}withholding tax")
        je.credit(cash, abs(amount_b), f"cash {c.currency}{note}")
        je.source_kind = "dividend"
    elif "dividend" in typ or "lieu" in typ or "payment in lieu" in typ:
        je.debit(cash, amount_b, f"cash {c.currency}{note}")
        je.credit(cfg.account("dividend_income"), amount_b, f"{sym}dividend")
        je.source_kind = "dividend"
    elif "interest" in typ:
        if amount_b >= 0:   # interest received
            je.debit(cash, amount_b, f"cash {c.currency}{note}")
            je.credit(cfg.account("interest_income"), amount_b, "interest received")
        else:               # interest paid
            je.debit(cfg.account("interest_expense"), abs(amount_b), "interest paid")
            je.credit(cash, abs(amount_b), f"cash {c.currency}{note}")
        je.source_kind = "interest"
    elif "deposit" in typ or "withdrawal" in typ:
        # cash transfers sometimes arrive as CashTransaction rather than DW
        je.signed(cash, amount_b, f"cash {c.currency}{note}")
        je.signed(cfg.account("equity"), -amount_b, "capital transfer")
        je.source_kind = "deposit"
    else:
        # Fees, commission adjustments, anything else → fees account.
        je.signed(cash, amount_b, f"cash {c.currency}{note}")
        je.signed(cfg.account("fees"), -amount_b, f"{c.type or 'fee'}")
        je.source_kind = "fee"

    return je.finalize()


def translate_deposit(d: DepositEvent, cfg: BookkeepingConfig) -> JournalEntry | None:
    base = cfg.base_currency
    amount_b = _to_base(d.amount, d.fx_rate_to_base)
    if amount_b == 0:
        return None
    note = _ccy_note(d.amount, d.currency, base, d.fx_rate_to_base)
    kind = "deposit" if d.amount > 0 else "withdrawal"
    je = JournalEntry(
        date=d.date,
        reference=f"IBKR:{d.external_id}",
        comment=f"{kind} {d.description}".strip()[:200],
        currency=base,
        source_kind="deposit",
    )
    cash = cfg.cash_account(d.currency)
    # deposit: Dr cash / Cr equity ; withdrawal: reverse (signed handles it)
    je.signed(cash, amount_b, f"cash {d.currency}{note}")
    je.signed(cfg.account("equity"), -amount_b, f"capital {kind}")
    return je.finalize()


def translate_day(day: FlexDay, cfg: BookkeepingConfig) -> List[JournalEntry]:
    """Translate a whole Flex day, skipping (and logging) any event that fails."""
    entries: List[JournalEntry] = []
    for t in day.trades:
        _safe_append(entries, translate_trade, t, cfg, ("trade", t.external_id))
    for c in day.cash:
        _safe_append(entries, translate_cash, c, cfg, ("cash", c.external_id))
    for d in day.deposits:
        _safe_append(entries, translate_deposit, d, cfg, ("deposit", d.external_id))
    log.info("day_translated", entries=len(entries), events=day.total_events)
    return entries


def _safe_append(out, fn, event, cfg, ident) -> None:
    try:
        je = fn(event, cfg)
        if je is not None:
            out.append(je)
    except Exception as e:
        log.warning("translate_failed", kind=ident[0], external_id=ident[1], error=str(e))
