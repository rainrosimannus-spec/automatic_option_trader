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

from datetime import datetime, timezone

from ib_insync import Forex, LimitOrder, MarketOrder, Stock

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


def etf_market_open(now: datetime | None = None) -> bool:
    """True when Xetra + LSE are both open (weekday, ~08:00–16:30 UTC) so the park ETFs can fill.

    Only the parking legs gate on this — the USD debit-close (IDEALPRO FX) runs 24/5 and ignores it.
    Used so a restart-triggered run outside market hours does the debit-close but defers parking to the
    daily 14:00 UTC slot (which is inside this window) instead of firing orders that can't fill.
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:                       # Sat/Sun
        return False
    minutes = now.hour * 60 + now.minute
    return 8 * 60 <= minutes <= 16 * 60 + 30     # 08:00–16:30 UTC (LSE window ⊂ Xetra window)


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
    """Qualify the canonical IBKR FX pair for {base, ccy}, trying base-first then ccy-first.

    Returns (contract, reason). `contract` is None when neither orientation qualifies, and
    `reason` then carries the REAL error from IBKR.

    Previously every exception here was swallowed (`except Exception: cand = None`) and the
    caller reported a hard-coded "(FX permission?)" guess. That made a genuine permissions
    problem indistinguishable from a transient connection / event-loop error, and left a live
    USD margin loan open with no usable diagnostic. EUR.USD is the canonical IBKR pair and
    always exists, so a failure here is never really "no such pair".
    """
    from src.broker.connection import get_ib_lock
    errors: list[str] = []
    with get_ib_lock():
        for sym, cur in ((base, ccy), (ccy, base)):
            pair_name = sym + cur
            cand = Forex(pair_name)
            try:
                ib.qualifyContracts(cand)
            except Exception as e:
                errors.append(f"{pair_name}: {type(e).__name__}: {e}")
                log.warning("fx_pair_qualify_failed", pair=pair_name, base=base, ccy=ccy,
                            error_type=type(e).__name__, error=str(e) or repr(e))
                continue
            if getattr(cand, "conId", 0):
                return (cand, "")
            errors.append(f"{pair_name}: qualified but conId=0")
            log.warning("fx_pair_no_conid", pair=pair_name, base=base, ccy=ccy)
    return (None, "; ".join(errors) or "no error reported by IBKR")


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


def _park_contract(symbol: str, exchange: str, currency: str, primary: str | None):
    """Build the park-ETF contract. A primaryExchange makes IBKR SMART-route to that listing (e.g. XFFE
    on LSEETF) instead of direct-routing — direct routes need a live market-data subscription and stick
    in PendingSubmit; SMART fills without one (same path AZN uses)."""
    if primary:
        return Stock(symbol, exchange, currency, primaryExchange=primary)
    return Stock(symbol, exchange, currency)


def _etf_last_price(ib, symbol: str, exchange: str, currency: str,
                    primary: str | None = None) -> float | None:
    """Best-effort last price for a park ETF (2-day daily bar). None on failure."""
    from src.broker.connection import get_ib_lock
    try:
        with get_ib_lock():
            contract = _park_contract(symbol, exchange, currency, primary)
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
                     shares: int, wait_secs: float, limit_price: float | None = None,
                     primary: str | None = None) -> bool:
    """Place a BUY/SELL on a park ETF (options connection). True if it filled.

    SMART-routes via `primary` (primaryExchange) so LSE-listed ETFs (XFFE) fill without a direct-route
    market-data subscription. Uses a marketable LIMIT when limit_price is given (crosses the spread),
    else a market order."""
    from src.broker.connection import get_ib_lock
    try:
        with get_ib_lock():
            contract = _park_contract(symbol, exchange, currency, primary)
            ib.qualifyContracts(contract)
            order = (LimitOrder(action, shares, round(limit_price, 2))
                     if limit_price and limit_price > 0 else MarketOrder(action, shares))
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


def _convert_to_ccy(ib, cfg, base: str, ccy: str, need_ccy: float, eur_liquid: float,
                    eur_working: float, dry: bool) -> tuple:
    """Raise `need_ccy` of `ccy` by converting EUR (selling XEON first if liquid EUR is short).

    Returns (ok, detail_str, deferred).
      - ok=True                    → converted (or a sub-minimum leg intentionally left to IBKR auto-FX).
      - ok=False, deferred=True    → NOT a failure: the EUR is parked in XEON and that sale can't free it
                                     right now (ETF market closed / didn't fill), so the whole conversion
                                     is postponed to the next pass. The FX leg is NOT placed — selling EUR
                                     we haven't freed just earns an Error 201 "currency leverage" rejection
                                     and reads as a false alarm. A restart in European hours retries it.
      - ok=False, deferred=False   → a real failure (caller alerts).
    """
    pair, pair_err = _qualify_fx_pair(ib, base, ccy)
    if pair is None:
        return (False, f"{base}.{ccy} qualify failed — {pair_err}", False)
    rate = _fx_rate_ccy_per_base(ib, pair, base, ccy)   # ccy per 1 base
    plan = fx_conversion_plan(base, ccy, need_ccy, rate, pair.symbol,
                              cfg.fx_idealpro_min_base)
    if not plan["place"]:
        if plan["reason"] == "below_min":
            return (True, f"leg €{plan['base_value']:,.0f} < IDEALPRO min — left to IBKR auto-FX", False)
        if plan["reason"] == "no_rate":
            return (True, "unpriced — left to IBKR auto-FX", False)
        return (True, plan["reason"], False)

    need_eur = plan["base_value"]
    # The FX order sells base_value × the plan's 1.01 buffer — that many euros must be PRESENT or IBKR
    # rejects the leg as currency leverage (Error 201; no Client-Portal toggle enables it). Fund it from
    # the EUR that's ALREADY liquid above the working buffer first, then free only the REMAINDER by selling
    # XEON. This keeps the working pool at its target ("stays as is"), NEVER double-sells euros a prior
    # (failed) pass already freed to liquid, and never tries to sell more XEON than is parked. XEON trades
    # European hours only; if the top-up sale can't be placed/filled, DEFER (don't place the FX leg against
    # euros we haven't freed) and retry next in-hours pass.
    fx_eur = need_eur * 1.01                             # euros the plan's SELL order actually moves
    xeon_to_raise = fx_eur + eur_working - eur_liquid    # remainder after spending liquid down to the buffer
    park_note = ""
    if xeon_to_raise > 1.0:
        if not etf_market_open():
            return (False, f"need €{xeon_to_raise:,.0f} more from {cfg.fx_park_eur_symbol}, but ETF market "
                           f"closed — deferred to next in-hours pass", True)
        _eur_primary = getattr(cfg, "fx_park_eur_primary", "") or None
        price = _etf_last_price(ib, cfg.fx_park_eur_symbol,
                                cfg.fx_park_eur_exchange, cfg.fx_park_eur_currency, primary=_eur_primary)
        if not (price and price > 0):
            return (False, f"could not price {cfg.fx_park_eur_symbol} to free €{xeon_to_raise:,.0f} "
                           f"— deferred", True)
        # Size for the 0.5% marketable-limit haircut so the freed euros fully cover the shortfall.
        shares = int(xeon_to_raise * 1.005 / (price * 0.995)) + 1
        if not dry:
            freed = _place_etf_order(ib, "SELL", cfg.fx_park_eur_symbol, cfg.fx_park_eur_exchange,
                                     cfg.fx_park_eur_currency, shares, cfg.fx_fill_wait_secs,
                                     limit_price=price * 0.995, primary=_eur_primary)
            if not freed:
                return (False, f"{cfg.fx_park_eur_symbol} sale to free €{xeon_to_raise:,.0f} did not fill "
                               f"— deferred", True)
        park_note = (f"; sold {shares} {cfg.fx_park_eur_symbol} (≈€{shares * price:,.0f}) to top up — "
                     f"liquid working pool €{eur_working:,.0f} kept")

    detail = (f"{plan['action']} {plan['qty']} {pair.symbol}{pair.currency} "
              f"(≈€{need_eur:,.0f} → {ccy} {need_ccy:,.0f}){park_note}")
    if dry:
        return (True, detail, False)

    from src.broker.connection import get_ib_lock
    with get_ib_lock():
        trade = ib.placeOrder(pair, MarketOrder(plan["action"], plan["qty"]))
    status, filled = _wait_fill(ib, trade, cfg.fx_fill_wait_secs)
    if status == "Filled" or filled >= plan["qty"] * 0.9:
        return (True, detail + f" [filled {status}]", False)
    try:
        with get_ib_lock():
            ib.cancelOrder(trade.order)
    except Exception:
        pass
    return (False, detail + f" [UNFILLED {status}]", False)


def crash_regime_active() -> bool:
    """True when the system crash detector is currently flagged active (persisted SystemState
    'crash_active' in the shared trades.db, written daily by the options RiskManager and mirrored
    in MarsWalk). Used ONLY by the PORTFOLIO/compounder debit-close (buyer.manage_fx_treasury): in a
    crash the compounder INTENTIONALLY levers up (capitulation margin) to buy the drawdown, so its
    debt must not be paid down — that would de-lever exactly at the leverage-buy moment. The OPTIONS
    debit-close does NOT use this (its debit is negative carry to clear regardless of regime).
    Fail-safe: any read error → treat as NOT a crash (portfolio closes debt as normal)."""
    try:
        from src.core.database import get_db
        from src.core.models import SystemState
        with get_db() as db:
            row = db.query(SystemState).filter(SystemState.key == "crash_active").first()
            return bool(row and str(row.value).strip().lower() == "true")
    except Exception:
        return False


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
        eur_cash = cash.get(base, 0.0)

        alerts = None
        try:
            from src.core.alerts import get_alert_manager
            alerts = get_alert_manager()
        except Exception:
            pass

        # ── 1. Debit auto-close for ANY non-base currency (self-sizing, one-directional) ──
        # The wheel settles in USD but the options universe also holds CAD/GBP/HKD/AUD names; a
        # foreign assignment draws that currency negative into a margin loan financed by idle EUR
        # (the un-closed CAD loan). Close the LARGEST such debit this pass and return — act-once-
        # per-pass (the next daily run takes the next currency), so `eur_cash`/`eur_working` never
        # need re-reading mid-pass. Sizes EUR→ccy to bring the debit up to a small positive buffer;
        # never converts ccy→EUR, so each currency float grows to exactly the book's footprint.
        # NOTE: the options debit-close runs even in a crash — the USD (or other non-base) debit here is
        # negative carry (borrow USD ~5.5% financed by idle EUR ~2%), and idle EUR is NOT the wheel's
        # crash fuel, so clearing it is always correct. (The PORTFOLIO debit-close DOES stand down in a
        # crash — there the debt is intentional capitulation leverage; see buyer.manage_fx_treasury.)
        debits = []
        for _ccy, _bal in cash.items():
            if _ccy == base:
                continue
            _dc = plan_debit_close(_bal, nlv, cfg.fx_debit_close_threshold_pct,
                                   cfg.fx_settlement_buffer_pct)
            if _dc["act"]:
                debits.append((_ccy, _bal, _dc))
        if debits:
            debits.sort(key=lambda x: x[1])          # most-negative balance (largest debit) first
            ccy, ccy_cash, dc = debits[0]
            eur_working = nlv * cfg.fx_eur_working_pct   # liquid EUR buffer to preserve (the "as is" pool)
            ok, detail, deferred = _convert_to_ccy(ib, cfg, base, ccy, dc["need_usd"], eur_cash,
                                                   eur_working, dry)
            log.info("fx_treasury_debit_close", dry_run=dry, ccy=ccy, ok=ok, deferred=deferred,
                     ccy_cash=round(ccy_cash), need_ccy=round(dc["need_usd"]),
                     nlv=round(nlv), detail=detail,
                     other_debits=[c for c, _, _ in debits[1:]])
            if deferred:
                # Expected pre-open condition (EUR still parked in XEON) — retry next pass, no alarm.
                log.info("fx_treasury_debit_close_deferred", ccy=ccy, detail=detail)
            elif alerts:
                title = f"{ccy} debit auto-close"
                msg = (f"{ccy} balance {ccy_cash:,.0f} (debit) → target +{nlv * cfg.fx_settlement_buffer_pct:,.0f}\n"
                       f"{detail}\n{'OK' if ok else 'FAILED — check manually'}")
                (alerts.treasury_alert(title, msg, dry_run=dry) if ok
                 else alerts.critical(f"FX treasury: {ccy} debit close FAILED", msg))
            return   # act once per pass; park next run

        # ── 2. Park idle cash per currency (EUR→XEON, USD→XFFE) — only when the ETF markets are open ──
        # (a restart-triggered run at, say, 22:00 UTC skips parking and lets the daily 14:00 slot do it,
        # rather than firing orders that can't fill; the debit-close above already ran regardless.)
        if not etf_market_open():
            log.info("fx_treasury_park_skipped_market_closed")
            return
        parked_notes = []
        legs = [
            (base, eur_cash, cfg.fx_eur_working_pct, cfg.fx_park_eur_symbol,
             cfg.fx_park_eur_exchange, cfg.fx_park_eur_currency,
             getattr(cfg, "fx_park_eur_primary", "") or None),
        ]
        # USD leg (XFFE) DISABLED 2026-07-02: XFFE can't be validated/routed without a live LSEETF
        # market-data subscription — orders place then stick in PendingSubmit (whyHeld='', filled=0),
        # same wall as RACE/BVME. The park is worth only ~$55/yr over IBKR's own ~4.3% credit on idle USD,
        # so USD simply stays as cash. The EUR→XEON leg and the USD debit-close (IDEALPRO FX) are unaffected.
        if getattr(cfg, "fx_park_usd_enabled", True):
            legs.append(("USD", cash.get("USD", 0.0), cfg.fx_usd_working_pct, cfg.fx_park_usd_symbol,
                         cfg.fx_park_usd_exchange, cfg.fx_park_usd_currency,
                         getattr(cfg, "fx_park_usd_primary", "") or None))
        for ccy, liquid, working_pct, sym, exch, etf_ccy, primary in legs:
            amount = plan_park(liquid, nlv, working_pct, cfg.fx_min_park_amount)
            if amount <= 0:
                continue
            price = _etf_last_price(ib, sym, exch, etf_ccy, primary=primary)
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
                # Marketable limit 1.5% THROUGH the last daily close + SMART route via primary. The wide
                # cap is needed because thin LSE ETFs (XFFE) quote well above the stale daily-close we size
                # from — a 0.5% cap didn't cross the live ask and stuck in PendingSubmit. A limit fills at
                # the touch (ask), not the cap, so this doesn't overpay; it just guarantees it crosses.
                _place_etf_order(ib, "BUY", sym, exch, etf_ccy, shares, cfg.fx_fill_wait_secs,
                                 limit_price=price * 1.015, primary=primary)

        if parked_notes and alerts:
            alerts.treasury_alert("Cash parked", "\n".join(parked_notes), dry_run=dry)

    except Exception as e:
        log.error("fx_treasury_error", error=str(e))
