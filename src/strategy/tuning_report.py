"""
#6 Tuning scaffolding — read-only performance report to inform delta and
profit-take tuning of the cash-machine put-selling strategy.

Buckets closed short-put positions by entry delta and reports the outcome
metrics needed to decide whether a higher entry delta (more premium) pays for
itself net of assignment friction, and how fast positions are exiting. Pure
read — places no orders, changes no config. Reads delta_at_entry / iv_at_entry
on Trade, so no schema change is required.

READINESS NOTE (the one remaining step for full #6):
    In *suggestion mode* the executed entry is created by the IBKR trade-sync,
    not by put_seller._record_trade, so Trade.delta_at_entry / iv_at_entry are
    currently NULL for those rows and this report stays empty until they are
    populated. Entry delta + IV-rank are now carried on each suggestion's
    `signal` text ("delta=0.234 DTE=2 ivr=55 x1") as the bridge. To finish #6,
    persist those onto the executed Trade (either parse the signal in the
    suggestion executor, or add a structured `delta` column to TradeSuggestion
    and copy it across). Deferred here on purpose: it touches the shared
    suggestion executor / a live-DB migration and should be a deliberate step.
    delta_at_entry IS already populated for live-mode (non-suggestion) trades.

Run:  python3 -m src.strategy.tuning_report [days_back]
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, TradeType
from src.core.logger import get_logger

log = get_logger(__name__)

# Entry-delta bucket upper bounds — bands around the 0.20-0.30 base.
_DELTA_BUCKETS = [0.15, 0.20, 0.25, 0.30, 0.40, 1.01]


def _bucket_label(delta: float) -> str:
    lo = 0.0
    for hi in _DELTA_BUCKETS:
        if delta < hi:
            return f"{lo:.2f}-{hi:.2f}"
        lo = hi
    return f">{_DELTA_BUCKETS[-1]:.2f}"


def build_delta_performance_report(days_back: int = 180) -> dict:
    """Per-entry-delta-bucket outcome stats for closed short puts.

    For each bucket: count, win_rate (expired worthless OR realized_pnl > 0),
    assignment_rate, avg/total realized P&L, avg days held, avg entry premium,
    avg IV at entry. Use to judge whether a higher entry delta's extra premium
    outweighs its higher assignment rate (#6 delta tuning) and whether exits
    are fast enough (avg days held).
    """
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    buckets: dict[str, dict] = {}

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
            entry = (
                db.query(Trade)
                .filter(
                    Trade.position_id == pos.id,
                    Trade.trade_type == TradeType.SELL_PUT,
                )
                .order_by(Trade.created_at.asc())
                .first()
            )
            delta = entry.delta_at_entry if entry and entry.delta_at_entry is not None else None
            if delta is None:
                continue
            label = _bucket_label(abs(delta))
            b = buckets.setdefault(label, {
                "count": 0, "wins": 0, "assigned": 0,
                "realized_pnl": 0.0, "days_held": 0.0,
                "entry_premium": 0.0, "iv_sum": 0.0, "iv_n": 0,
            })
            b["count"] += 1
            if pos.status == PositionStatus.ASSIGNED:
                b["assigned"] += 1
            if pos.status == PositionStatus.EXPIRED or (pos.realized_pnl or 0) > 0:
                b["wins"] += 1
            b["realized_pnl"] += (pos.realized_pnl or 0.0)
            if pos.opened_at and pos.closed_at:
                b["days_held"] += max((pos.closed_at - pos.opened_at).total_seconds() / 86400.0, 0)
            b["entry_premium"] += (pos.entry_premium or 0.0)
            if entry and entry.iv_at_entry is not None:
                b["iv_sum"] += entry.iv_at_entry
                b["iv_n"] += 1

    report: dict[str, dict] = {}
    for label, b in sorted(buckets.items()):
        n = b["count"] or 1
        report[label] = {
            "count": b["count"],
            "win_rate": round(b["wins"] / n, 3),
            "assignment_rate": round(b["assigned"] / n, 3),
            "avg_realized_pnl": round(b["realized_pnl"] / n, 2),
            "total_realized_pnl": round(b["realized_pnl"], 2),
            "avg_days_held": round(b["days_held"] / n, 2),
            "avg_entry_premium": round(b["entry_premium"] / n, 4),
            "avg_iv_at_entry": round(b["iv_sum"] / b["iv_n"], 4) if b["iv_n"] else None,
        }
    return report


def print_report(days_back: int = 180) -> None:
    rep = build_delta_performance_report(days_back)
    if not rep:
        print(f"No closed short-put positions with entry delta in the last {days_back} days.")
        return
    print(f"\nShort-put performance by entry delta (last {days_back} days)")
    print(f"{'delta band':<12}{'n':>5}{'win%':>7}{'assign%':>9}"
          f"{'avg P&L':>10}{'tot P&L':>11}{'days':>7}{'avg prem':>10}{'avg IV':>8}")
    for label, m in rep.items():
        print(f"{label:<12}{m['count']:>5}{m['win_rate'] * 100:>6.0f}%"
              f"{m['assignment_rate'] * 100:>8.0f}%{m['avg_realized_pnl']:>10.2f}"
              f"{m['total_realized_pnl']:>11.2f}{m['avg_days_held']:>7.1f}"
              f"{m['avg_entry_premium']:>10.4f}"
              f"{(m['avg_iv_at_entry'] if m['avg_iv_at_entry'] is not None else 0):>8.3f}")
    print("\n#6 read: judge whether a higher entry delta's extra premium "
          "outweighs its higher assignment rate, and whether exits are fast "
          "enough (avg days held). No orders placed; this is read-only.")


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    print_report(days)
