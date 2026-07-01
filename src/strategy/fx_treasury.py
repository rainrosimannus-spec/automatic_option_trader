"""FX treasury for the EUR-base options account.

The options account (Maggy, U25878705) is **EUR-base** but the wheel sells and gets assigned in
**USD**. Left alone, USD assignments run the account into a **USD margin loan** (a debit) financed by
idle EUR — borrowing dollars at ~5.5-6% while euros earn ~2%, a recurring negative carry.

This module runs once daily and does two things (both **one-directional** and **self-sizing**, so they
scale untouched from €200k to €2M+ NLV):

1. **Auto-close the USD debit.** If USD cash is a debit beyond a small % of NLV, convert JUST ENOUGH
   EUR→USD to bring USD back to a small positive buffer. It NEVER converts USD→EUR, so the USD balance
   naturally grows to exactly the wheel's USD footprint over cycles — no static float to maintain.

2. **Park idle cash per currency.** Idle EUR above a working buffer → XEON (EUR money-market UCITS ETF);
   idle USD above a working buffer → XFFE (USD money-market UCITS ETF, LSE USD line). Both are UCITS
   (have a KID), so they clear the PRIIPs wall that blocks SGOV on this EU account. Both are ~95%+
   marginable, so parking does NOT reduce excess liquidity and cannot starve the wheel's collateral.

Currency policy is **match-the-business, not yield-chase**: the ~2% USD>EUR rate gap is covered-interest
parity (an FX-risk premium, not free alpha), so we hold USD only for what the wheel needs and keep the
rest in the EUR base. All amounts are % of NLV — there are no dollar constants.

**Double-gated + dry-run first.** Does nothing unless `risk.fx_treasury_enabled`; even then places NO
orders while `risk.fx_treasury_dry_run` (burn-in: logs + alerts exactly what it WOULD do). Real-money FX
is alerted on every conversion.

All order placement uses the OPTIONS connection (src.broker.connection get_ib/get_ib_lock). The pure
decision helpers (`plan_debit_close`, `plan_park`, `fx_conversion_plan`) take no IBKR access so they are
unit-tested directly.
"""
from __future__ import annotations

from ib_insync import Forex, MarketOrder, Stock

from src.core.config import get_settings
from src.core.logger import get_logger

log = get_logger("strategy.fx_treasury")


# ─────────────────────────── pure decision helpers (unit-tested) ───────────────────────────

def plan_debit_close(usd_cash: float, nlv: float,
                     threshold_pct: float, buffer_pct: float) -> dict:
    """Decide whether to close a USD debit and how much USD to raise.

    Acts only when USD is a debit LARGER than `threshold_pct` of NLV (skips trivial/transient debits
    that self-cure when the wheel's next call-away returns dollars). When it acts, it raises enough to
    bring USD from its negative balance up to `buffer_pct` of NLV (a small positive settlement cushion).

    Returns {act, need_usd, reason}. need_usd is in USD.
    """
    if nlv <= 0:
        return {"act": False, "need_usd": 0.0, "reason": "no_nlv"}
    threshold = nlv * max(0.0, threshold_pct)
    if usd_cash >= -threshold:
        return {"act": False, "need_usd": 0.0, "reason": "within_threshold"}
    buffer = nlv * max(0.0, buffer_pct)
    need_usd = (-usd_cash) + buffer          # cover the debit + leave a small positive cushion
    return {"act": True, "need_usd": need_usd, "reason": "debit"}


def plan_park(liquid_cash: float, nlv: float,
              working_pct: float, min_amount: float) -> float:
    """Amount of a currency's LIQUID cash to sweep into its park ETF.

    Keep `working_pct` of NLV liquid (unparked) for settlement/conversions; park the excess above that,
    but only if the excess clears `min_amount` (avoid dust round-trips). Returns 0.0 when nothing to do.
    """
    if nlv <= 0 or liquid_cash <= 0:
        return 0.0
    working = nlv * max(0.0, working_pct)
    excess = liquid_cash - working
    return excess if excess >= max(0.0, min_amount) else 0.0


def fx_conversion_plan(base: str, ccy: str, shortfall_ccy: float, rate_ccy_per_base: float,
                       pair_symbol: str, idealpro_min_base: float,
                       min_convert: float = 1000.0) -> dict:
    """Pure BUY/SELL + qty decision for an IDEALPRO conversion (no IBKR access).

    Mirrors src.portfolio.buyer._fx_conversion_plan (kept as a local copy to avoid importing the heavy
    portfolio module into the options strategy). IBKR exposes only ONE direction per pair (e.g. EUR.USD,
    never USD.EUR), so qty is always denominated in the pair's SYMBOL currency and action derives from
    which side the pair is quoted. Returns {place, action, qty, base_value, reason}.
    """
    base = (base or "").upper()
    ccy = (ccy or "").upper()
    if shortfall_ccy <= 0 or shortfall_ccy < min_convert:
        return {"place": False, "action": None, "qty": 0, "base_value": 0.0, "reason": "funded"}
    if not rate_ccy_per_base or rate_ccy_per_base <= 0:
        return {"place": False, "action": None, "qty": 0, "base_value": 0.0, "reason": "no_rate"}
    base_value = shortfall_ccy / rate_ccy_per_base
    if base_value < idealpro_min_base:
        return {"place": False, "action": None, "qty": 0, "base_value": base_value, "reason": "below_min"}
    buf = 1.01
    if (pair_symbol or "").upper() == ccy:
        # Canonical pair is ccy.base (symbol == ccy) → BUY ccy directly; qty denominated in ccy.
        return {"place": True, "action": "BUY", "qty": int(round(shortfall_ccy * buf)),
                "base_value": base_value, "reason": "convert"}
    # Canonical pair is base.ccy (symbol == base) → SELL base to receive ccy; qty denominated in base.
    return {"place": True, "action": "SELL", "qty": int(round(base_value * buf)),
            "base_value": base_value, "reason": "convert"}


# ─────────────────────────── IBKR-touching helpers (options connection) ───────────────────────────

def _per_currency_cash(ib) -> dict:
    """Map of {CURRENCY: cash balance} from the options account (skips the BASE roll-up row)."""
    from src.broker.connection import get_ib_lock
    out: dict[str, float] = {}
    with get_ib_lock():
        vals = ib.accountValues()
    for v in vals:
        if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
            try:
                out[v.currency.upper()] = float(v.value)
            except (ValueError, TypeError):
                continue
    return out


def _fx_rate_ccy_per_base(ib, pair, base: str, ccy: str) -> float:
    """FX mid for `pair` as units of `ccy` per 1 unit of `base`. 0.0 on any failure."""
    from src.broker.connection import get_ib_lock
    try:
        with get_ib_lock():
            t = ib.reqMktData(pair, "", True, False)
            ib.sleep(2)
        px = None
        for cand in (t.midpoint(), t.marketPrice(), t.last, t.close):
            if cand and cand == cand and cand > 0:
                px = float(cand)
                break
        try:
            with get_ib_lock():
                ib.cancelMktData(pair)
        except Exception:
            pass
        if not px:
            return 0.0
        if (pair.symbol or "").upper() == (base or "").upper():
            return px
        return 1.0 / px
    except Exception:
        return 0.0


def _qualify_fx_pair(ib, base: str, ccy: str):
    """Qualify the canonical IBKR FX pair for {base, ccy}, trying base-first then ccy-first."""
    from src.broker.connection import get_ib_lock
    with get_ib_lock():
        for sym, cur in ((base, ccy), (ccy, base)):
            cand = Forex(sym + cur)
            try:
                ib.qualifyContracts(cand)
            except Exception:
                cand = None
            if cand is not None and getattr(cand, "conId", 0):
                return cand
    return None


def _wait_fill(ib, trade, wait_secs: float) -> tuple:
    """Block up to wait_secs for a terminal order state. Returns (status, filled)."""
    from src.broker.connection import get_ib_lock
    waited = 0.0
    while waited < wait_secs:
        with get_ib_lock():
            ib.sleep(1.0)
        waited += 1.0
        st = trade.orderStatus.status or ""
        if st in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            break
    return (trade.orderStatus.status or "", float(trade.orderStatus.filled or 0.0))


def _etf_last_price(ib, symbol: str, exchange: str, currency: str) -> float | None:
    """Best-effort last price for a park ETF (2-day daily bar). None on failure."""
    from src.broker.connection import get_ib_lock
    try:
        with get_ib_lock():
            contract = Stock(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract, endDateTime="", durationStr="2 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=False, formatDate=1, timeout=10,
            )
        if bars and bars[-1].close > 0:
            return float(bars[-1].close)
    except Exception as e:
        log.warning("fx_treasury_etf_price_failed", symbol=symbol, error=str(e))
    return None


def _place_etf_order(ib, action: str, symbol: str, exchange: str, currency: str,
                     shares: int, wait_secs: float) -> bool:
    """Place a market BUY/SELL on a park ETF (options connection). True if it filled."""
    from src.broker.connection import get_ib_lock
    try:
        with get_ib_lock():
            contract = Stock(symbol, exchange, currency)
            ib.qualifyContracts(contract)
            order = MarketOrder(action, shares)
            order.tif = "DAY"
            order.outsideRth = False
            trade = ib.placeOrder(contract, order)
        status, filled = _wait_fill(ib, trade, wait_secs)
        if status == "Filled" or filled >= shares * 0.9:
            log.info("fx_treasury_etf_filled", action=action, symbol=symbol,
                     shares=shares, status=status)
            return True
        try:
            with get_ib_lock():
                ib.cancelOrder(trade.order)
        except Exception:
            pass
        log.warning("fx_treasury_etf_unfilled", action=action, symbol=symbol,
                    shares=shares, status=status, filled=filled)
        return False
    except Exception as e:
        log.warning("fx_treasury_etf_order_failed", action=action, symbol=symbol, error=str(e))
        return False


def _convert_to_usd(ib, cfg, base: str, need_usd: float, eur_liquid: float, dry: bool) -> tuple:
    """Raise `need_usd` of USD by converting EUR (selling XEON first if liquid EUR is short).

    Returns (ok, detail_str). ok=False only when a real, above-minimum conversion could not be placed
    or did not fill (fail-closed — caller alerts; nothing worse than the pre-existing debit happens).
    """
    pair = _qualify_fx_pair(ib, base, "USD")
    if pair is None:
        return (False, "no EUR.USD pair (FX permission?)")
    rate = _fx_rate_ccy_per_base(ib, pair, base, "USD")   # USD per 1 EUR
    plan = fx_conversion_plan(base, "USD", need_usd, rate, pair.symbol,
                              cfg.fx_idealpro_min_base)
    if not plan["place"]:
        if plan["reason"] == "below_min":
            return (True, f"leg €{plan['base_value']:,.0f} < IDEALPRO min — left to IBKR auto-FX")
        if plan["reason"] == "no_rate":
            return (True, "unpriced — left to IBKR auto-FX")
        return (True, plan["reason"])

    need_eur = plan["base_value"]
    # If liquid EUR can't cover the conversion, sell XEON to raise the shortfall first.
    park_note = ""
    if eur_liquid < need_eur:
        short_eur = need_eur - eur_liquid
        price = _etf_last_price(ib, cfg.fx_park_eur_symbol,
                                cfg.fx_park_eur_exchange, cfg.fx_park_eur_currency)
        if price and price > 0:
            shares = int(short_eur / price) + 1
            park_note = f"; sell {shares} {cfg.fx_park_eur_symbol} to free €{short_eur:,.0f}"
            if not dry:
                _place_etf_order(ib, "SELL", cfg.fx_park_eur_symbol, cfg.fx_park_eur_exchange,
                                 cfg.fx_park_eur_currency, shares, cfg.fx_fill_wait_secs)
        else:
            park_note = f"; WARN could not price {cfg.fx_park_eur_symbol} to free €{short_eur:,.0f}"

    detail = (f"{plan['action']} {plan['qty']} {pair.symbol}{pair.currency} "
              f"(≈€{need_eur:,.0f} → ${need_usd:,.0f}){park_note}")
    if dry:
        return (True, detail)

    from src.broker.connection import get_ib_lock
    with get_ib_lock():
        trade = ib.placeOrder(pair, MarketOrder(plan["action"], plan["qty"]))
    status, filled = _wait_fill(ib, trade, cfg.fx_fill_wait_secs)
    if status == "Filled" or filled >= plan["qty"] * 0.9:
        return (True, detail + f" [filled {status}]")
    try:
        with get_ib_lock():
            ib.cancelOrder(trade.order)
    except Exception:
        pass
    return (False, detail + f" [UNFILLED {status}]")


# ─────────────────────────── orchestrator (scheduler entry point) ───────────────────────────

def manage_fx_treasury() -> None:
    """Daily FX-treasury pass: close any USD debit, else park idle cash per currency.

    Priority: a USD debit-close and parking never run in the same pass (avoids intra-run churn) —
    close the debit today, park next run. No-op unless enabled; places no orders while dry_run.
    """
    cfg = get_settings().risk
    if not cfg.fx_treasury_enabled:
        return

    from src.broker.connection import get_ib, is_connected
    if not is_connected():
        return
    try:
        ib = get_ib()
    except Exception:
        return

    dry = bool(cfg.fx_treasury_dry_run)
    base = (cfg.fx_base_currency or "EUR").upper()

    try:
        from src.broker.account import get_account_summary
        summary = get_account_summary()
        nlv = summary.net_liquidation
        if nlv <= 0:
            log.info("fx_treasury_skip_no_nlv")
            return

        cash = _per_currency_cash(ib)
        usd_cash = cash.get("USD", 0.0)
        eur_cash = cash.get(base, 0.0)

        alerts = None
        try:
            from src.core.alerts import get_alert_manager
            alerts = get_alert_manager()
        except Exception:
            pass

        # ── 1. USD debit-close (self-sizing, one-directional) ──
        dc = plan_debit_close(usd_cash, nlv, cfg.fx_debit_close_threshold_pct,
                              cfg.fx_settlement_buffer_pct)
        if dc["act"]:
            eur_working = nlv * cfg.fx_eur_working_pct
            ok, detail = _convert_to_usd(ib, cfg, base, dc["need_usd"], eur_cash, dry)
            log.info("fx_treasury_debit_close", dry_run=dry, ok=ok,
                     usd_cash=round(usd_cash), need_usd=round(dc["need_usd"]),
                     nlv=round(nlv), detail=detail)
            if alerts:
                title = "USD debit auto-close"
                msg = (f"USD balance ${usd_cash:,.0f} (debit) → target +${nlv * cfg.fx_settlement_buffer_pct:,.0f}\n"
                       f"{detail}\n{'OK' if ok else 'FAILED — check manually'}")
                (alerts.treasury_alert(title, msg, dry_run=dry) if ok
                 else alerts.critical("FX treasury: USD debit close FAILED", msg))
            return   # act once per pass; park next run

        # ── 2. Park idle cash per currency (EUR→XEON, USD→XFFE) ──
        parked_notes = []
        legs = [
            (base, eur_cash, cfg.fx_eur_working_pct, cfg.fx_park_eur_symbol,
             cfg.fx_park_eur_exchange, cfg.fx_park_eur_currency),
            ("USD", usd_cash, cfg.fx_usd_working_pct, cfg.fx_park_usd_symbol,
             cfg.fx_park_usd_exchange, cfg.fx_park_usd_currency),
        ]
        for ccy, liquid, working_pct, sym, exch, etf_ccy in legs:
            amount = plan_park(liquid, nlv, working_pct, cfg.fx_min_park_amount)
            if amount <= 0:
                continue
            price = _etf_last_price(ib, sym, exch, etf_ccy)
            if not price or price <= 0:
                log.warning("fx_treasury_park_no_price", symbol=sym)
                continue
            shares = int(amount / price)
            if shares < 1:
                continue
            note = f"park {ccy} {amount:,.0f} → BUY {shares} {sym}"
            parked_notes.append(note)
            log.info("fx_treasury_park", dry_run=dry, ccy=ccy, symbol=sym,
                     amount=round(amount), shares=shares)
            if not dry:
                _place_etf_order(ib, "BUY", sym, exch, etf_ccy, shares, cfg.fx_fill_wait_secs)

        if parked_notes and alerts:
            alerts.treasury_alert("Cash parked", "\n".join(parked_notes), dry_run=dry)

    except Exception as e:
        log.error("fx_treasury_error", error=str(e))
