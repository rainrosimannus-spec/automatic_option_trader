#!/bin/bash
# ============================================================
# Options Trader — Start Script
# Runs pre-flight checks then launches the trader
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "⚡ Options Trader — Launching"
echo ""

# Activate venv
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
source .venv/bin/activate

# Pre-flight checks
echo "Pre-flight checks:"

# Check TWS port is reachable (Options Trader account on port 4001)
if nc -z 127.0.0.1 4001 2>/dev/null; then
    echo "  ✅ Options Trader TWS port 4001 is open"
else
    echo "  ⚠️  Cannot reach Options Trader TWS on port 4001"
    echo "     Make sure TWS for account U23886415 is running"
    echo "     with API enabled on port 4001"
    echo ""
    read -p "  Continue anyway? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Check config
if [ ! -f "config/.env" ]; then
    echo "  ❌ config/.env missing. Run ./setup.sh first."
    exit 1
fi
echo "  ✅ Configuration files present"

# Check trading mode
MODE=$(grep TRADING_MODE config/.env | cut -d= -f2 | tr -d ' ')
if [ "$MODE" = "live" ]; then
    echo ""
    echo "  ⚠️  WARNING: TRADING_MODE=live"
    echo "     This will place REAL trades with REAL money!"
    read -p "  Are you sure? Type 'yes' to confirm: " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo "  Aborted. Set TRADING_MODE=paper in config/.env"
        exit 1
    fi
else
    echo "  ✅ Paper trading mode"
fi

echo ""
echo "Starting trader... Dashboard at http://localhost:8000"
echo "Press Ctrl+C to stop"
echo "────────────────────────────────────────────"
echo ""

# Launch
python -m src.main
