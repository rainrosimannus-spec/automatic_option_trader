"""Measure the BSM-vs-market pricing gap on 0-7 DTE OTM puts.

The MarsWalk engine prices synthetic options via Black-Scholes with the
daily-close OPTION_IMPLIED_VOLATILITY. Real markets quote 0-3 DTE OTM puts
much richer (vol smile + term structure + bid floor + min tick). The
existing `pricing.effective_iv()` applies a scalar `SHORT_DTE_K` IV uplift
to compensate — but k=4.95 was a single-point fit against son's live result,
not a measurement of the actual gap.

This script measures the gap directly. For each universe symbol it:
  1. Pulls the option chain for 0-7 DTE OTM puts (delta ~ 0.05-0.40).
  2. Reads the live bid/ask via reqMktData (same as live screen_puts does
     for the chosen contract).
  3. Computes the BSM theoretical mid at the cached daily IV.
  4. Bisects for the empirical IV multiplier `x` such that
     BS(spot, K, T, daily_iv * x).mid ≈ real_mid.
  5. Writes one JSONL row per qualifying contract.

After the snapshot, the median `x` per DTE bucket is the empirical
short-DTE IV-uplift curve — replaces the k=4.95 fudge factor with a measured
value.

Run via the /marswalk/calibrate-pricing endpoint (must be in-process per
the ibkr-access-in-process-locked memory).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from src.core.logger import get_logger
from src.broker.greeks import compute_put_greeks, get_current_iv
from src.broker.market_data import get_put_contracts, get_stock_price
from src.strategy.universe import UniverseManager
from src.portfolio.connection import (
    get_portfolio_ib, get_portfolio_lock, _ensure_event_loop,
)

log = get_logger("calibration.short_dte")

DTE_MIN = 0
DTE_MAX = 7
DELTA_MIN = 0.05
DELTA_MAX = 0.40
# Liquidity filters — skip junk contracts.
MIN_BID = 0.05
MAX_SPREAD_ABS = 0.50    # ask - bid in dollars
MAX_SPREAD_PCT = 0.50    # (ask - bid) / mid


def _solve_iv_multiplier(spot: float, strike: float, T: float, daily_iv: float,
                         target_mid: float) -> float | None:
    """Bisect for x such that compute_put_greeks(spot, K, T, daily_iv*x).mid ≈ target_mid.

    Returns x in [0.1, 20.0] or None if no convergence."""
    if daily_iv <= 0 or target_mid <= 0 or spot <= 0 or strike <= 0 or T <= 0:
        return None
    lo, hi = 0.1, 20.0
    # Verify the target is reachable in this range.
    g_lo = compute_put_greeks(spot, strike, T, daily_iv * lo)
    g_hi = compute_put_greeks(spot, strike, T, daily_iv * hi)
    if g_lo is None or g_hi is None:
        return None
    if g_lo.mid > target_mid or g_hi.mid < target_mid:
        # Out of bracket. Either market price is below pure-BSM at very low
        # IV (rare for OTM puts) or above BSM at 20x IV (extreme).
        return None
    for _ in range(40):
        mid_x = 0.5 * (lo + hi)
        g = compute_put_greeks(spot, strike, T, daily_iv * mid_x)
        if g is None:
            return None
        if abs(g.mid - target_mid) < 0.005:
            return round(mid_x, 4)
        if g.mid < target_mid:
            lo = mid_x
        else:
            hi = mid_x
    return round(0.5 * (lo + hi), 4)


def _snapshot_one_symbol(ib, symbol: str, exchange: str, currency: str,
                        opt_exchange: str) -> list[dict]:
    """Pull chains + live quotes for one symbol, return measurement rows."""
    out: list[dict] = []
    try:
        spot = get_stock_price(symbol, exchange=exchange, currency=currency)
        if not spot or spot <= 0:
            return out
        daily_iv = get_current_iv(ib, symbol, exchange=exchange, currency=currency)
        if not daily_iv or daily_iv <= 0:
            return out
        contracts = get_put_contracts(symbol, exchange=opt_exchange, currency=currency,
                                      min_dte=DTE_MIN, max_dte=DTE_MAX)
        if not contracts:
            return out
    except Exception as e:
        log.warning("calibration_setup_failed", symbol=symbol, error=str(e))
        return out

    today = datetime.now().date()
    for c in contracts:
        try:
            exp_date = datetime.strptime(c.lastTradeDateOrContractMonth, "%Y%m%d").date()
            dte = (exp_date - today).days
            if dte < DTE_MIN or dte > DTE_MAX:
                continue
            strike = float(c.strike)
            if strike >= spot:  # we want OTM puts only
                continue
            T = max(dte, 0.25) / 365.0
            theo = compute_put_greeks(spot, strike, T, daily_iv)
            if theo is None or theo.delta is None:
                continue
            abs_delta = abs(theo.delta)
            if abs_delta < DELTA_MIN or abs_delta > DELTA_MAX:
                continue
            # Pull live quote — same path as live screener.
            with get_portfolio_lock():
                ticker = ib.reqMktData(c, "", True, False)
                ib.sleep(1.0)
                ib.cancelMktData(c)
            bid = ticker.bid
            ask = ticker.ask
            if not (bid and bid > 0 and bid != -1.0):
                continue
            if not (ask and ask > 0 and ask != -1.0):
                continue
            spread = ask - bid
            if bid < MIN_BID:
                continue
            if spread > MAX_SPREAD_ABS:
                continue
            mid = round((bid + ask) / 2, 4)
            if mid > 0 and spread / mid > MAX_SPREAD_PCT:
                continue
            x = _solve_iv_multiplier(spot, strike, T, daily_iv, mid)
            if x is None:
                continue
            row = {
                "symbol": symbol,
                "dte": dte,
                "strike": strike,
                "spot": round(spot, 4),
                "moneyness": round(strike / spot, 4),
                "daily_iv": round(daily_iv, 4),
                "delta": round(theo.delta, 4),
                "bid": round(bid, 4),
                "ask": round(ask, 4),
                "mid_real": mid,
                "mid_bs": round(theo.mid, 4),
                "x": x,                                  # empirical IV multiplier
                "ratio": round(mid / theo.mid, 4) if theo.mid > 0 else None,
            }
            out.append(row)
        except Exception as e:
            log.warning("calibration_contract_failed", symbol=symbol,
                        strike=getattr(c, "strike", "?"), error=str(e))
            continue
    return out


def run_calibration() -> dict:
    """Snapshot the universe, write to data/pricing_calibration_<YYYYMMDD>.jsonl,
    and return a summary dict.

    Must run in-process — uses get_portfolio_ib() / get_portfolio_lock() per
    ibkr-access-in-process-locked memory."""
    _ensure_event_loop()
    ib = get_portfolio_ib()
    if ib is None:
        return {"status": "error", "msg": "portfolio IB not connected"}

    universe = UniverseManager()
    symbols = list(universe.all_symbols)

    out_path = Path("data") / f"pricing_calibration_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_rows = 0
    with out_path.open("a") as fh:
        for sym in symbols:
            exchange = universe.get_exchange(sym) if hasattr(universe, "get_exchange") else "SMART"
            currency = universe.get_currency(sym) if hasattr(universe, "get_currency") else "USD"
            opt_exchange = universe.get_options_exchange(sym) if hasattr(universe, "get_options_exchange") else exchange
            rows = _snapshot_one_symbol(ib, sym, exchange, currency, opt_exchange)
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            n_rows += len(rows)
            log.info("calibration_symbol_done", symbol=sym, rows=len(rows))

    summary = summarize_jsonl(out_path)
    summary["path"] = str(out_path)
    summary["status"] = "ok"
    summary["rows"] = n_rows
    return summary


def summarize_jsonl(path: str | Path) -> dict:
    """Read a calibration JSONL and report median x per DTE bucket."""
    path = Path(path)
    if not path.exists():
        return {"status": "error", "msg": f"no such file: {path}"}

    buckets: dict[int, list[float]] = {d: [] for d in range(DTE_MIN, DTE_MAX + 1)}
    total = 0
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            dte = row.get("dte")
            x = row.get("x")
            if dte is None or x is None:
                continue
            if dte in buckets:
                buckets[dte].append(float(x))
                total += 1

    summary_rows = []
    for dte in sorted(buckets):
        xs = sorted(buckets[dte])
        n = len(xs)
        if n == 0:
            summary_rows.append({"dte": dte, "n": 0})
            continue
        median = xs[n // 2]
        q1 = xs[n // 4] if n >= 4 else xs[0]
        q3 = xs[(3 * n) // 4] if n >= 4 else xs[-1]
        summary_rows.append({
            "dte": dte, "n": n,
            "median_x": round(median, 3),
            "q1": round(q1, 3), "q3": round(q3, 3),
            "iqr": round(q3 - q1, 3),
        })
    return {"per_dte": summary_rows, "total_rows": total}
