"""Pure wheel exit predicates shared by the live trader and the MarsWalk backtester.

Two rules Rain set on 2026-11-04 after the options account's first four months
(EUR 205k vs EUR 220k deposited; nine assigned lots = 1.5x NLV in stock, -$92k):

* STOP — an assigned lot is sold outright once it trades `stop_pct` below the
  assignment strike. Holding the bags cost $37k more than selling the next day
  over the first 36 assignments; a -8% stop would have cut the stock loss from
  -$86k to -$51k while the velocity covered call still runs on every lot above it.
* PACKAGE UNWIND — Rain's manual rule the code lacked: on a covered lot, as soon as
  the package (stock bid minus the call's ask) is above the lot's net basis, buy the
  call back and sell the shares together. Gives up a little extrinsic, frees the
  capital weeks before expiry (VRT 2026-10-29, CTAS 10-27, ISRG 10-07 by hand).

Both are side-effect-free so live (`wheel.py`, `profit_taker.py`) and the sim
(`marswalk/engine.py`) judge the same arithmetic ([[live-marswalk-parity-rule]]).
"""
from __future__ import annotations


def stop_loss_triggered(mark: float | None, assign_strike: float | None, stop_pct: float) -> bool:
    """True when the lot's mark is at/below assignment strike x (1 - stop_pct).

    Fails CLOSED on a missing/zero mark or strike (never sells on no data)."""
    if not mark or mark <= 0 or not assign_strike or assign_strike <= 0 or stop_pct <= 0:
        return False
    return mark <= assign_strike * (1.0 - stop_pct)


def package_unwind_net_basis(cost_basis: float, realized_cc_per_share: float,
                             open_cc_premium: float) -> float:
    """Net basis of a covered lot = cost basis (strike - put premium) minus CC
    premium already realized per share minus the open call's own premium (cash in
    hand; counted once here, not again in the package)."""
    return float(cost_basis) - float(realized_cc_per_share or 0.0) - float(open_cc_premium or 0.0)


def package_unwind_triggered(stock_bid: float | None, call_ask: float | None,
                             cost_basis: float | None, realized_cc_per_share: float,
                             open_cc_premium: float, *, min_profit_pct: float,
                             fees_per_share: float, pad: float = 0.05) -> bool:
    """Rain's package rule: fire when selling the shares at `stock_bid` after buying
    the call back at `call_ask + pad` (the order's limit) and paying `fees_per_share`
    clears the lot's net basis by at least `min_profit_pct` of the cost basis.

        stock_bid - (call_ask + pad) - fees - net_basis >= min_profit_pct * cost_basis

    Fails CLOSED on a missing/zero bid, ask or basis."""
    if not stock_bid or stock_bid <= 0 or call_ask is None or call_ask <= 0:
        return False
    if not cost_basis or cost_basis <= 0:
        return False
    net_basis = package_unwind_net_basis(cost_basis, realized_cc_per_share, open_cc_premium)
    package = stock_bid - (call_ask + pad) - fees_per_share
    return (package - net_basis) >= min_profit_pct * cost_basis
