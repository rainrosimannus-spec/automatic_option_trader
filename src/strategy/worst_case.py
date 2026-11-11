"""Total worst-case cap (Rain, 2026-11-11): one stress scenario over EVERYTHING open.

Containment for the whole book, not per position. Each open position gets a worst-case
loss under one stress day; the sum plus the candidate may not exceed cap_pct × NLV.

Stress model (deliberately simple and readable):
  single-name short put   strike × 100 × qty × single_gap      (the name gaps `single_gap` through the strike)
  held stock lot          mark × shares × stock_gap            (held names fall `stock_gap`; call premium ignored)
  index put SPREAD        (short − wing − net credit) × 100 × qty   (defined: the width minus what was collected)
  index put NAKED         strike × 100 × qty × index_gap       (the index gaps `index_gap`)

Pure functions here are shared by the live gate (RiskManager.check_worst_case_cap /
worst_case_clamp), the index sleeve sizing, the MarsWalk engine and the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorstCaseBook:
    single_puts: float = 0.0
    stock: float = 0.0
    index: float = 0.0
    detail: list[dict] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.single_puts + self.stock + self.index


def stress_single_put(strike: float, qty: int, single_gap: float) -> float:
    return max(0.0, float(strike or 0) * 100.0 * int(qty or 0) * float(single_gap))


def stress_stock(mark: float, shares: int, stock_gap: float) -> float:
    return max(0.0, float(mark or 0) * int(shares or 0) * float(stock_gap))


def stress_index_spread(short_strike: float, wing: float, net_credit: float, qty: int) -> float:
    width = max(0.0, float(short_strike) - float(wing))
    return max(0.0, (width - max(0.0, float(net_credit or 0))) * 100.0 * int(qty or 0))


def stress_index_naked(strike: float, qty: int, index_gap: float) -> float:
    return max(0.0, float(strike or 0) * 100.0 * int(qty or 0) * float(index_gap))


def book_worst_case(puts: list[dict], stocks: list[dict], index_puts: list[dict], cfg) -> WorstCaseBook:
    """puts: [{symbol, strike, qty}], stocks: [{symbol, mark, shares}],
    index_puts: [{symbol, strike, qty, wing|None, net_credit}]."""
    b = WorstCaseBook()
    for p in puts:
        v = stress_single_put(p["strike"], p["qty"], cfg.single_gap)
        b.single_puts += v; b.detail.append({"kind": "put", "symbol": p.get("symbol"), "loss": round(v)})
    for s in stocks:
        v = stress_stock(s["mark"], s["shares"], cfg.stock_gap)
        b.stock += v; b.detail.append({"kind": "stock", "symbol": s.get("symbol"), "loss": round(v)})
    for i in index_puts:
        if i.get("wing"):
            v = stress_index_spread(i["strike"], i["wing"], i.get("net_credit", 0.0), i["qty"])
        else:
            v = stress_index_naked(i["strike"], i["qty"], cfg.index_gap)
        b.index += v; b.detail.append({"kind": "index", "symbol": i.get("symbol"), "loss": round(v)})
    return b


def headroom(book_total: float, nlv: float, cap_pct: float) -> float:
    """Dollars of worst-case loss still allowed. Negative = already over the cap."""
    return float(cap_pct) * float(nlv) - float(book_total)


def contracts_within(headroom_usd: float, loss_per_contract: float, wanted: int) -> int:
    """Largest qty ≤ wanted whose stress fits the headroom (0 when none fits)."""
    if wanted <= 0 or loss_per_contract <= 0:
        return max(0, int(wanted)) if loss_per_contract <= 0 else 0
    return max(0, min(int(wanted), int(headroom_usd // loss_per_contract)))
