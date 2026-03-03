"""
Import Real Positions — one-time import of existing IBKR option positions.

Reads all open option positions from IBKR and creates tracking records so the
system can monitor them to expiry. No new trades are initiated on these positions
— they simply wind down naturally.

Usage:
    python -m tools.import_positions              # dry-run (show what would import)
    python -m tools.import_positions --execute    # actually import into DB

What it imports:
  - Short puts  → options trader Position table (status=open, type=short_put)
  - Short calls → options trader Position table (status=open, type=covered_call)
  - Long puts   → options trader Position table (status=open, type=long_put_hedge)
  - CSPs that match portfolio watchlist → PortfolioPutEntry table too

What it does NOT do:
  - Does NOT place any orders
  - Does NOT initiate new positions
  - Does NOT modify existing positions in IBKR
  - Imported positions are marked source="imported" so the system knows
    not to manage them actively (no rolling, no profit-taking)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, PortfolioItem


def connect_ibkr(host: str = "127.0.0.1", port: int = 4001,
                 client_id: int = 50) -> IB:
    """Connect to IBKR."""
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=30, readonly=True)
    print(f"✅ Connected to IBKR (port {port}, client {client_id})")
    return ib


def fetch_option_positions(ib: IB) -> list[dict]:
    """Fetch all option positions from IBKR and parse them."""
    positions = ib.portfolio()
    options = []

    for pos in positions:
        c = pos.contract
        if c.secType != "OPT":
            continue

        qty = int(pos.position)
        if qty == 0:
            continue

        entry = {
            "symbol": c.symbol,
            "secType": c.secType,
            "right": c.right,           # "P" or "C"
            "strike": c.strike,
            "expiry": c.lastTradeDateOrContractMonth,
            "exchange": c.exchange or "SMART",
            "currency": c.currency or "USD",
            "multiplier": int(c.multiplier or 100),
            "quantity": qty,            # negative = short, positive = long
            "avg_cost": pos.averageCost,
            "market_price": pos.marketPrice,
            "market_value": pos.marketValue,
            "unrealized_pnl": pos.unrealizedPNL,
            "con_id": c.conId,
        }

        # Classify
        if qty < 0 and c.right == "P":
            entry["position_type"] = "short_put"
        elif qty < 0 and c.right == "C":
            entry["position_type"] = "covered_call"
        elif qty > 0 and c.right == "P":
            entry["position_type"] = "long_put_hedge"
        elif qty > 0 and c.right == "C":
            entry["position_type"] = "long_call"
        else:
            entry["position_type"] = "unknown"

        options.append(entry)

    return options


def display_positions(options: list[dict]):
    """Pretty-print discovered positions."""
    if not options:
        print("\n📭 No option positions found in IBKR.")
        return

    print(f"\n📋 Found {len(options)} option positions:\n")
    print(f"{'Symbol':<8} {'Type':<16} {'Strike':>8} {'Expiry':<10} {'Qty':>5} "
          f"{'Avg Cost':>10} {'Mkt Value':>10} {'P&L':>10}")
    print("─" * 90)

    for o in sorted(options, key=lambda x: (x["symbol"], x["expiry"])):
        pnl = o["unrealized_pnl"] or 0
        pnl_str = f"${pnl:>+9,.2f}"
        print(f"{o['symbol']:<8} {o['position_type']:<16} "
              f"${o['strike']:>7.2f} {o['expiry']:<10} {o['quantity']:>5} "
              f"${o['avg_cost']:>9.2f} ${o['market_value']:>9.2f} {pnl_str}")


def import_to_options_tracker(options: list[dict]):
    """Import positions into the options trader Position table."""
    from src.core.database import get_db
    from src.core.models import Position, PositionStatus

    imported = 0
    skipped = 0

    with get_db() as db:
        for o in options:
            # Skip long calls (not part of our strategy)
            if o["position_type"] == "long_call":
                skipped += 1
                continue

            # Check if already imported (by symbol + strike + expiry)
            existing = db.query(Position).filter(
                Position.symbol == o["symbol"],
                Position.strike == o["strike"],
                Position.expiry == o["expiry"],
                Position.status == PositionStatus.OPEN,
            ).first()

            if existing:
                print(f"  ⏭  {o['symbol']} {o['strike']} {o['expiry']} — already tracked")
                skipped += 1
                continue

            # Determine premium from avg_cost
            # IBKR avg_cost for options = price per share (not per contract)
            premium_per_share = abs(o["avg_cost"]) / o["multiplier"] if o["avg_cost"] else 0

            position = Position(
                symbol=o["symbol"],
                status=PositionStatus.OPEN,
                position_type=o["position_type"],
                strike=o["strike"],
                expiry=o["expiry"],
                entry_premium=round(premium_per_share, 4),
                quantity=abs(o["quantity"]),
                total_premium_collected=round(premium_per_share * abs(o["quantity"]) * o["multiplier"], 2),
                realized_pnl=0.0,
                is_wheel=False,
            )
            db.add(position)
            imported += 1
            print(f"  ✅ Imported {o['position_type']}: {o['symbol']} "
                  f"${o['strike']} exp {o['expiry']} x{abs(o['quantity'])}")

    print(f"\n📊 Options tracker: {imported} imported, {skipped} skipped")
    return imported


def import_to_portfolio_puts(options: list[dict]):
    """
    Import short puts that match portfolio watchlist stocks into PortfolioPutEntry.
    These are CSPs that were sold to enter long-term positions.
    """
    from src.core.database import get_db
    from src.portfolio.models import PortfolioPutEntry, PortfolioWatchlist

    imported = 0
    skipped = 0

    short_puts = [o for o in options if o["position_type"] == "short_put"]
    if not short_puts:
        print("\n📭 No short puts to import to portfolio tracker.")
        return 0

    with get_db() as db:
        # Get all watchlist symbols
        watchlist = db.query(PortfolioWatchlist).all()
        watchlist_symbols = {w.symbol: w for w in watchlist}

        for o in short_puts:
            # Only import if stock is in portfolio watchlist
            wl = watchlist_symbols.get(o["symbol"])
            if not wl:
                continue  # not a portfolio stock — skip

            # Check if already imported
            existing = db.query(PortfolioPutEntry).filter(
                PortfolioPutEntry.symbol == o["symbol"],
                PortfolioPutEntry.strike == o["strike"],
                PortfolioPutEntry.expiry == o["expiry"],
                PortfolioPutEntry.status == "open",
            ).first()

            if existing:
                print(f"  ⏭  Portfolio CSP {o['symbol']} {o['strike']} {o['expiry']} — already tracked")
                skipped += 1
                continue

            premium = abs(o["avg_cost"]) / o["multiplier"] if o["avg_cost"] else 0
            contracts = abs(o["quantity"])

            entry = PortfolioPutEntry(
                symbol=o["symbol"],
                tier=wl.tier,
                exchange=o["exchange"],
                currency=o["currency"],
                strike=o["strike"],
                expiry=o["expiry"],
                contracts=contracts,
                premium=round(premium, 4),
                total_premium=round(premium * contracts * o["multiplier"], 2),
                status="open",
                effective_cost=round(o["strike"] - premium, 2),
            )
            db.add(entry)

            # Mark watchlist entry as having open put
            wl.has_open_put = True
            wl.put_strike = o["strike"]
            wl.put_expiry = o["expiry"]
            wl.target_buy_price = round(o["strike"] - premium, 2)

            imported += 1
            print(f"  ✅ Portfolio CSP: {o['symbol']} ${o['strike']} "
                  f"exp {o['expiry']} x{contracts} (tier: {wl.tier})")

    print(f"\n📊 Portfolio puts: {imported} imported, {skipped} skipped")
    return imported


def main():
    parser = argparse.ArgumentParser(description="Import IBKR option positions into tracker")
    parser.add_argument("--execute", action="store_true",
                        help="Actually import (default is dry-run)")
    parser.add_argument("--host", default="127.0.0.1", help="IBKR host")
    parser.add_argument("--port", type=int, default=4001, help="IBKR port")
    parser.add_argument("--client-id", type=int, default=50, help="IBKR client ID")
    args = parser.parse_args()

    print("=" * 60)
    print("  IBKR Position Importer")
    print(f"  Mode: {'🔴 EXECUTE' if args.execute else '🟡 DRY RUN'}")
    print("=" * 60)

    # Connect
    ib = connect_ibkr(args.host, args.port, args.client_id)

    try:
        # Fetch
        options = fetch_option_positions(ib)
        display_positions(options)

        if not options:
            return

        if not args.execute:
            print(f"\n🟡 DRY RUN — no changes made.")
            print(f"   Run with --execute to import these positions.")
            return

        # Import
        print(f"\n🔴 Importing positions...\n")

        # 1. Options tracker (all option positions)
        opt_count = import_to_options_tracker(options)

        # 2. Portfolio put entries (CSPs matching watchlist)
        put_count = import_to_portfolio_puts(options)

        print(f"\n✅ Import complete:")
        print(f"   Options tracker: {opt_count} positions")
        print(f"   Portfolio CSPs:  {put_count} positions")
        print(f"\n   These will track to expiry. No new trades will be initiated on them.")

    finally:
        ib.disconnect()
        print("\n📡 Disconnected from IBKR.")


if __name__ == "__main__":
    main()
