"""
#6 Tuning scaffolding — read-only performance report to inform delta and
profit-take tuning of the cash-machine put-selling strategy.

Buckets closed short-put positions by entry delta AND by entry VIX, and crosses
them (delta x VIX matrix), reporting win rate / realized P&L / assignment rate /
days-held — the data needed to decide whether a higher entry delta (more premium)
pays for itself net of assignment friction, and whether that answer differs by
volatility regime (the "higher delta in LOW vol" hypothesis). Pure read — places
no orders, changes no config. Reads delta_at_entry / iv_at_entry / vix_at_entry
on Trade, so no schema change is required.

DATA SOURCES & THE REMAINING GAP:
    In *suggestion mode* the executed entry is created by the IBKR trade-sync, not
    by put_seller._record_trade, so Trade.delta_at_entry / iv_at_entry /
    vix_at_entry are NULL. Entry DELTA is recovered read-only via _signal_delta()
    by matching the originating suggestion and parsing its `signal` ("delta=0.204
    DTE=3"). VIX and IV-rank are NOT in the real signals (delta+DTE only), so the
    VIX report / delta x VIX matrix stay empty until vix_at_entry is actually
    captured on executed trades. That capture (persist delta+VIX onto the executed
    Trade in the suggestion executor) is the deferred step — it touches the shared
    executor + a live-DB migration, so it's a deliberate future change, not done
    here. delta/vix_at_entry ARE populated for live-mode (non-suggestion) trades.

Run:  python3 -m src.strategy.tuning_report [days_back]
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, TradeType
from src.core.suggestions import TradeSuggestion
from src.core.logger import get_logger

_SIGNAL_DELTA_RE = re.compile(r"delta=([0-9.]+)")

log = get_logger(__name__)

# Entry-delta bucket upper bounds — bands around the 0.20-0.30 base.
_DELTA_BUCKETS = [0.15, 0.20, 0.25, 0.30, 0.40, 1.01]
# VIX regime bands.
_VIX_BUCKETS = [13, 15, 18, 22, 30, 999]


def _bucket_label(value: float, edges: list[float], fmt: str) -> str:
    lo = 0.0
    for hi in edges:
        if value < hi:
            return f"{lo:{fmt}}-{hi:{fmt}}"
        lo = hi
    return f">{edges[-1]:{fmt}}"


def _delta_label(delta: float) -> str:
    return _bucket_label(delta, _DELTA_BUCKETS, ".2f")


def _vix_label(vix: float) -> str:
    return _bucket_label(vix, _VIX_BUCKETS, ".0f")


def _signal_delta(db, pos) -> float | None:
    """Recover entry delta for a suggestion-mode position by matching its
    originating TradeSuggestion (symbol/strike/expiry) and parsing the signal
    ('delta=0.204 DTE=3'). If several match, take the one created closest to the
    position's open time. Read-only — no DB write."""
    sugs = (
        db.query(TradeSuggestion.signal, TradeSuggestion.created_at)
        .filter(
            TradeSuggestion.symbol == pos.symbol,
            TradeSuggestion.strike == pos.strike,
            TradeSuggestion.expiry == pos.expiry,
            TradeSuggestion.signal.isnot(None),
        )
        .all()
    )
    best, best_gap = None, None
    for s in sugs:
        m = _SIGNAL_DELTA_RE.search(s.signal or "")
        if not m:
            continue
        d = float(m.group(1))
        gap = abs((s.created_at - pos.opened_at).total_seconds()) if (pos.opened_at and s.created_at) else 0.0
        if best is None or gap < best_gap:
            best, best_gap = d, gap
    return best


def _collect_outcomes(days_back: int) -> list[dict]:
    """One pass over closed short puts -> per-position outcome rows. Shared by
    every view below so the win/assignment/P&L logic lives in one place."""
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    rows: list[dict] = []
    with get_db() as db:
        positions = (
            db.query(Position)
            .filter(
                Position.position_type == "short_put",
                Position.status.in_([
                    PositionStatus.CLOSED,
                    PositionStatus.EXPIRED,
                    PositionStatus.ASSIGNED,
                ]),
                Position.opened_at >= cutoff,
            )
            .all()
        )
        for pos in positions:
            # Column-scoped (not full-ORM) so it works even before the bid/ask/
            # mid_at_entry migration has run on the live DB (model has the columns,
            # the table may not until the next restart) — and it's cheaper.
            entry = (
                db.query(Trade.delta_at_entry, Trade.iv_at_entry, Trade.vix_at_entry)
                .filter(
                    Trade.position_id == pos.id,
                    Trade.trade_type == TradeType.SELL_PUT,
                )
                .order_by(Trade.created_at.asc())
                .first()
            )
            delta = entry.delta_at_entry if entry and entry.delta_at_entry is not None else None
            delta_source = "trade"
            if delta is None:
                # Bridge: suggestion-mode trades have NULL delta_at_entry, but the
                # originating suggestion's signal carries it ("delta=0.204 DTE=3").
                # Read-only recovery (no DB write, no executor change).
                delta = _signal_delta(db, pos)
                delta_source = "signal"
            if delta is None:
                continue  # neither the trade nor a matching suggestion has entry delta
            days = None
            if pos.opened_at and pos.closed_at:
                days = max((pos.closed_at - pos.opened_at).total_seconds() / 86400.0, 0)
            rows.append({
                "delta": abs(delta),
                "delta_source": delta_source,
                "vix": entry.vix_at_entry if entry else None,
                "iv": entry.iv_at_entry if entry else None,
                "assigned": pos.status == PositionStatus.ASSIGNED,
                "win": pos.status == PositionStatus.EXPIRED or (pos.realized_pnl or 0) > 0,
                "pnl": pos.realized_pnl or 0.0,
                "days": days,
                "premium": pos.entry_premium or 0.0,
            })
    return rows


def _agg(rows: list[dict]) -> dict | None:
    n = len(rows)
    if n == 0:
        return None
    days = [r["days"] for r in rows if r["days"] is not None]
    ivs = [r["iv"] for r in rows if r["iv"] is not None]
    return {
        "count": n,
        "win_rate": round(sum(1 for r in rows if r["win"]) / n, 3),
        "assignment_rate": round(sum(1 for r in rows if r["assigned"]) / n, 3),
        "avg_realized_pnl": round(sum(r["pnl"] for r in rows) / n, 2),
        "total_realized_pnl": round(sum(r["pnl"] for r in rows), 2),
        "avg_days_held": round(sum(days) / len(days), 2) if days else None,
        "avg_entry_premium": round(sum(r["premium"] for r in rows) / n, 4),
        "avg_iv_at_entry": round(sum(ivs) / len(ivs), 4) if ivs else None,
    }


def _group(rows: list[dict], keyfn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        groups.setdefault(k, []).append(r)
    return {label: _agg(rs) for label, rs in sorted(groups.items())}


def build_delta_performance_report(days_back: int = 180) -> dict:
    """Per-entry-delta-bucket outcome stats for closed short puts."""
    return _group(_collect_outcomes(days_back), lambda r: _delta_label(r["delta"]))


def build_vix_performance_report(days_back: int = 180) -> dict:
    """Per-entry-VIX-bucket outcome stats (rows with no VIX are skipped)."""
    rows = [r for r in _collect_outcomes(days_back) if r["vix"] is not None]
    return _group(rows, lambda r: _vix_label(r["vix"]))


def build_delta_vix_matrix(days_back: int = 180) -> dict:
    """delta-band -> vix-band -> {count, win_rate, avg_realized_pnl}. The lens for
    'does a higher entry delta pay off specifically in low vol?'"""
    rows = [r for r in _collect_outcomes(days_back) if r["vix"] is not None]
    cells: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        cells.setdefault(_delta_label(r["delta"]), {}).setdefault(_vix_label(r["vix"]), []).append(r)
    out: dict[str, dict] = {}
    for dlabel, vmap in sorted(cells.items()):
        out[dlabel] = {}
        for vlabel, rs in vmap.items():
            a = _agg(rs)
            out[dlabel][vlabel] = {
                "count": a["count"], "win_rate": a["win_rate"],
                "avg_realized_pnl": a["avg_realized_pnl"],
            }
    return out


def _print_table(title: str, rep: dict) -> None:
    print(f"\n{title}")
    print(f"{'band':<12}{'n':>5}{'win%':>7}{'assign%':>9}"
          f"{'avg P&L':>10}{'tot P&L':>11}{'days':>7}{'avg prem':>10}{'avg IV':>8}")
    for label, m in rep.items():
        print(f"{label:<12}{m['count']:>5}{m['win_rate'] * 100:>6.0f}%"
              f"{m['assignment_rate'] * 100:>8.0f}%{m['avg_realized_pnl']:>10.2f}"
              f"{m['total_realized_pnl']:>11.2f}"
              f"{(m['avg_days_held'] if m['avg_days_held'] is not None else 0):>7.1f}"
              f"{m['avg_entry_premium']:>10.4f}"
              f"{(m['avg_iv_at_entry'] if m['avg_iv_at_entry'] is not None else 0):>8.3f}")


def print_report(days_back: int = 180) -> None:
    rows = _collect_outcomes(days_back)
    if not rows:
        print(f"\nNo closed short-put positions with entry delta in the last "
              f"{days_back} days. On this server most entries are suggestion-mode "
              f"(delta/vix_at_entry NULL) — see READINESS NOTE in this file.")
        return

    overall = _agg(rows)
    with_vix = sum(1 for r in rows if r["vix"] is not None)
    via_signal = sum(1 for r in rows if r["delta_source"] == "signal")
    print(f"\n=== #6 put-selling tuning report — last {days_back} days ===")
    print(f"closed short puts with entry delta: {overall['count']}  "
          f"(delta via suggestion-signal bridge: {via_signal}; with VIX: {with_vix})  "
          f"| overall win {overall['win_rate']*100:.0f}%  "
          f"assign {overall['assignment_rate']*100:.0f}%  "
          f"total P&L {overall['total_realized_pnl']:.2f}  "
          f"avg days held {overall['avg_days_held'] if overall['avg_days_held'] is not None else 0:.1f}")

    _print_table("By entry delta:", build_delta_performance_report(days_back))
    vrep = build_vix_performance_report(days_back)
    if vrep:
        _print_table("By entry VIX:", vrep)

    matrix = build_delta_vix_matrix(days_back)
    if matrix:
        vix_cols = [_vix_label(v - 0.001) for v in _VIX_BUCKETS]  # ordered band labels
        seen = []
        for d in matrix.values():
            for v in d:
                if v not in seen:
                    seen.append(v)
        vix_cols = [v for v in vix_cols if v in seen] or seen
        print("\ndelta x VIX  (cell = n / win% / avg P&L):")
        row_hdr = "delta\\vix"
        print(f"{row_hdr:<12}" + "".join(f"{c:>18}" for c in vix_cols))
        for dlabel, vmap in matrix.items():
            cells = []
            for c in vix_cols:
                cell = vmap.get(c)
                cells.append(f"{cell['count']}/{cell['win_rate']*100:.0f}%/{cell['avg_realized_pnl']:+.0f}"
                             if cell else "·")
            print(f"{dlabel:<12}" + "".join(f"{x:>18}" for x in cells))

    print("\n#6 read: judge whether a higher entry delta's extra premium beats its "
          "assignment friction — and whether that flips by vol regime (low-VIX "
          "cells). Watch the n per cell: thin samples (n<5) are directional only. "
          "No orders placed; read-only. For the counterfactual ('what if 0.30?'), "
          "use MarsWalk to backtest the change across regimes.")


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    print_report(days)
