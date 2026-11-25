"""
Shadow selector (Rain 2026-11-25): "what would the simulator have sold today?" — dry-run only.

Why: on the account's own 2026 window MarsWalk's picks printed +12% where the live screener's picks
did −5%, but the sim's own orderings disagree by 12-30 pp per window, so no backtest can say which
selector is better. This runs the sim's rule LIVE beside the screener, with real quotes at real scan
times, and books the result on paper so the two can be compared day by day
(`scripts/shadow_selector_report.py`).

The sim's rule (engine.Params defaults since 5eed7fc / c43600d):
  universe  = point-in-time S&P 500 ∪ Nasdaq-100 top-N by 0.6·rank(12m return) + 0.4·rank(20d $vol)
  order     = rank order, first qualifying (selection_mode="scan_order")
  contract  = strike nearest otm_pct below spot, longest expiry inside the live DTE window
  gates     = live VIX/DTE tier, earnings, not already held (live or shadow), real bid ≥ fee floor
  size      = qty contracts (1), compared per contract, never scaled to the account

Never places an order; writes data/shadow_selector.jsonl (kind: sell | settle | scan) and reads
nothing but IBKR quotes and FMP closes. Settlement = FMP close on the expiry date (same as the
XSP sleeve's settle job).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from src.core.config import get_settings
from src.core.database import get_db
from src.core.logger import get_logger
from src.core.models import Position, PositionStatus

log = get_logger("shadow_selector")


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────

def pick_contract(strikes_by_expiry: dict[str, list[float]], spot: float, otm_pct: float,
                  today: date) -> Optional[tuple[str, float]]:
    """(expiry, strike) the sim would take: the LONGEST expiry offered, strike nearest
    spot × (1 − otm_pct) among strikes below spot; ties → the higher strike (richer bid).
    Mirrors engine._pick_put_candidate in otm_pct mode."""
    if not strikes_by_expiry or not spot or spot <= 0:
        return None
    target = spot * (1.0 - otm_pct)

    def _dte(e):
        try:
            return (datetime.strptime(e, "%Y%m%d").date() - today).days
        except ValueError:
            return -1
    expiry = max(strikes_by_expiry, key=_dte)
    ks = [k for k in strikes_by_expiry[expiry] if 0 < k < spot]
    if not ks:
        return None
    return expiry, min(ks, key=lambda k: (abs(k - target), -k))


def slots_available(open_count: int, max_open: int, per_scan: int) -> int:
    return max(0, min(per_scan, max_open - open_count))


def settle_row(sell: dict, close: float, commission_per_contract: float) -> dict:
    """Paper settlement of one shadow put at the expiry close."""
    qty = int(sell.get("qty") or 1)
    intrinsic = max(0.0, float(sell["strike"]) - float(close))
    pnl = (float(sell["premium"]) - intrinsic) * 100 * qty - commission_per_contract * qty
    return {"kind": "settle", "sym": sell["sym"], "strike": sell["strike"], "expiry": sell["expiry"],
            "sold": sell["date"], "close": round(float(close), 4), "intrinsic": round(intrinsic, 4),
            "itm": intrinsic > 0, "qty": qty, "premium": sell["premium"], "pnl": round(pnl, 2)}


def _key(r: dict) -> tuple:
    return (r.get("sym"), float(r.get("strike") or 0), r.get("expiry"), r.get("sold") or r.get("date"))


def unsettled(rows: list[dict]) -> list[dict]:
    """Sell rows without a matching settle row."""
    done = {_key(r) for r in rows if r.get("kind") == "settle"}
    return [r for r in rows if r.get("kind") == "sell" and _key(r) not in done]


def read_jsonl(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_jsonl(path, row: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def fetch_dated(symbol: str, start: date, end: date) -> list[dict]:
    """Dated daily bars from FMP (fail-safe → [])."""
    try:
        from src.portfolio.fmp import _get
        data = _get("historical-price-eod/full", symbol, {"from": start.isoformat(), "to": end.isoformat()})
        return data if isinstance(data, list) else []
    except Exception:
        return []


def close_on_date(rows: list[dict], yyyymmdd: str) -> Optional[float]:
    want = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    for r in rows or []:
        if str(r.get("date", ""))[:10] == want and r.get("close") is not None:
            return float(r["close"])
    return None


# ── live runner ──────────────────────────────────────────────────────────────────────────────

class ShadowSelector:
    def __init__(self, universe, risk):
        self.universe = universe
        self.risk = risk
        self.cfg = get_settings().shadow_selector

    # universe -------------------------------------------------------------------------------
    def universe_list(self) -> tuple[list[str], str]:
        """(ranked names, source). Point-in-time screen when the store exists, else the live list."""
        try:
            from src.marswalk import pit_universe as pit
            names = pit.current_screen(self.cfg.universe_n)
            if names:
                latest = pit.latest_bar_date()
                if latest and (date.today() - date.fromisoformat(latest)).days > self.cfg.stale_after_days:
                    log.warning("shadow_universe_stale", latest_bar=latest)
                return names, f"pit:{latest}"
        except Exception as e:
            log.warning("shadow_universe_pit_failed", error=str(e))
        return list(self.universe.all_symbols), "live_universe"

    # state ----------------------------------------------------------------------------------
    def open_shadow(self) -> list[dict]:
        return unsettled(read_jsonl(self.cfg.log_path))

    @staticmethod
    def live_held_symbols(db) -> set[str]:
        rows = db.query(Position.symbol).filter(Position.status == PositionStatus.OPEN).all()
        return {r[0] for r in rows}

    # one scan -------------------------------------------------------------------------------
    def scan(self) -> dict:
        from src.strategy.put_seller import PutSeller
        from src.strategy.screener import _passes_fee_floor
        from src.broker.market_data import get_put_contracts, get_stock_price, note_option_feed
        from src.broker.connection import get_ib, get_ib_lock

        today = date.today()
        summary = {"kind": "scan", "ts": datetime.utcnow().isoformat(timespec="seconds"), "date": today.isoformat(),
                   "sold": [], "skipped": {}, "tried": 0}

        def skip(reason):
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

        dte = PutSeller(self.universe, self.risk)._resolve_dte("USD")
        if dte is None:
            summary["halt"] = "vix"
            log.info("shadow_scan_halt_vix")
            append_jsonl(self.cfg.log_path, summary)
            return summary
        dte_min, dte_max = dte
        names, source = self.universe_list()
        summary["universe"] = source
        open_rows = self.open_shadow()
        held = {r["sym"] for r in open_rows}
        with get_db() as db:
            held |= self.live_held_symbols(db)
        slots = slots_available(len(open_rows), self.cfg.max_open, self.cfg.per_scan)
        summary["slots"] = slots
        if slots <= 0:
            log.info("shadow_scan_no_slots", open=len(open_rows))
            append_jsonl(self.cfg.log_path, summary)
            return summary

        for rank, sym in enumerate(names, start=1):
            if slots <= 0 or summary["tried"] >= self.cfg.max_names_per_scan:
                break
            if sym in held:
                skip("held")
                continue
            summary["tried"] += 1
            try:
                if not self.risk.check_earnings(sym).allowed:
                    skip("earnings")
                    continue
            except Exception:
                pass
            try:
                spot = get_stock_price(sym, "SMART", "USD")
                if not spot or spot <= 0:
                    skip("no_spot")
                    continue
                contracts = get_put_contracts(sym, "SMART", "USD", max_dte=dte_max, min_dte=dte_min)
                if not contracts:
                    skip("no_chain")
                    continue
                by_exp: dict[str, list[float]] = {}
                for c in contracts:
                    by_exp.setdefault(c.lastTradeDateOrContractMonth, []).append(float(c.strike))
                pick = pick_contract(by_exp, spot, self.cfg.otm_pct, today)
                if not pick:
                    skip("no_strike")
                    continue
                expiry, strike = pick
                contract = next(c for c in contracts
                                if c.lastTradeDateOrContractMonth == expiry and abs(float(c.strike) - strike) < 1e-6)
                ib = get_ib()
                with get_ib_lock():
                    ticker = ib.reqMktData(contract, "", True, False)
                    ib.sleep(2)
                    ib.cancelMktData(contract)
                feed = note_option_feed(ticker)
                bid, ask = ticker.bid, ticker.ask
                ok_bid = bid and bid > 0 and bid != float("inf") and bid != -1.0
                ok_ask = ask and ask > 0 and ask != float("inf") and ask != -1.0
                if not ok_bid:
                    skip("no_bid")
                    continue
                if not _passes_fee_floor(bid, 100, "USD", 1):
                    skip("fee_floor")
                    continue
                row = {"kind": "sell", "ts": summary["ts"], "date": today.isoformat(), "sym": sym, "rank": rank,
                       "strike": strike, "expiry": expiry,
                       "dte": (datetime.strptime(expiry, "%Y%m%d").date() - today).days,
                       "spot": round(float(spot), 4), "otm_pct": round((1 - strike / spot) * 100, 2),
                       "premium": round(float(bid), 2), "ask": (round(float(ask), 2) if ok_ask else None),
                       "qty": self.cfg.qty, "feed": feed, "universe": source}
                append_jsonl(self.cfg.log_path, row)
                log.info("shadow_put_would_sell", **{k: v for k, v in row.items() if k not in ("kind", "ts")})
                summary["sold"].append(f"{sym} {strike}P {expiry} @ {row['premium']}")
                held.add(sym)
                slots -= 1
            except Exception as e:
                log.warning("shadow_scan_symbol_error", symbol=sym, error=str(e))
                skip("error")
        append_jsonl(self.cfg.log_path, summary)
        log.info("shadow_scan_done", sold=len(summary["sold"]), tried=summary["tried"],
                 skipped=summary["skipped"], universe=source)
        return summary

    # settlement -----------------------------------------------------------------------------
    def settle(self, today: date | None = None, fetch=fetch_dated) -> list[dict]:
        today = today or date.today()
        rows = read_jsonl(self.cfg.log_path)
        out = []
        cache: dict[str, list[dict]] = {}
        for sell in unsettled(rows):
            exp = datetime.strptime(sell["expiry"], "%Y%m%d").date()
            if exp >= today:
                continue
            if sell["sym"] not in cache:
                cache[sell["sym"]] = fetch(sell["sym"], exp - timedelta(days=6), exp + timedelta(days=1))
            close = close_on_date(cache[sell["sym"]], sell["expiry"])
            if close is None:
                log.info("shadow_settle_no_close_yet", symbol=sell["sym"], expiry=sell["expiry"])
                continue
            row = settle_row(sell, close, self.cfg.commission_per_contract)
            append_jsonl(self.cfg.log_path, row)
            out.append(row)
            log.info("shadow_put_settled", **{k: v for k, v in row.items() if k != "kind"})
        return out
