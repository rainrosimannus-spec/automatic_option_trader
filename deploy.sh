#!/bin/bash
set -e

echo "═══════════════════════════════════════════"
echo "  Deploying Options Trader v20"
echo "═══════════════════════════════════════════"

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Step 1: Stop everything
echo ""
echo "[1/5] Stopping any running instance..."
pkill -9 -f python 2>/dev/null || true
kill $(lsof -ti :8000) 2>/dev/null || true
sleep 3
echo "       Stopped."

# Step 2: Database fixes
echo "[2/5] Fixing database..."
if [ -f "data/trades.db" ]; then
    # Add missing columns
    sqlite3 data/trades.db "ALTER TABLE trade_suggestions ADD COLUMN opt_exchange VARCHAR(15);" 2>/dev/null || true
    sqlite3 data/trades.db "ALTER TABLE trade_suggestions ADD COLUMN opt_currency VARCHAR(5);" 2>/dev/null || true

    # Remove duplicate positions (keep lowest id per symbol+status+position_type)
    sqlite3 data/trades.db "DELETE FROM positions WHERE id NOT IN (SELECT MIN(id) FROM positions GROUP BY symbol, status, position_type);" 2>/dev/null || true

    # Remove duplicate trades (keep lowest id per ibkr_exec_id)
    sqlite3 data/trades.db "DELETE FROM trades WHERE ibkr_exec_id IS NOT NULL AND id NOT IN (SELECT MIN(id) FROM trades WHERE ibkr_exec_id IS NOT NULL GROUP BY ibkr_exec_id);" 2>/dev/null || true

    # Remove suggestion-source trades that have a matching IBKR-source trade (same symbol+strike+expiry+type)
    sqlite3 data/trades.db "DELETE FROM trades WHERE source = 'suggestion' AND EXISTS (SELECT 1 FROM trades t2 WHERE t2.source = 'ibkr_sync' AND t2.symbol = trades.symbol AND t2.strike = trades.strike AND t2.expiry = trades.expiry AND t2.trade_type = trades.trade_type);" 2>/dev/null || true

    # Remove snapshots with wrong NLV (portfolio NLV >100k leaked into options account)
    sqlite3 data/trades.db "DELETE FROM account_snapshots WHERE net_liquidation > 100000;" 2>/dev/null || true
    # Remove snapshots with zeroed NLV
    sqlite3 data/trades.db "DELETE FROM account_snapshots WHERE net_liquidation = 0.0;" 2>/dev/null || true

    echo "       Database cleaned."
else
    echo "       No database found (will be created on first start)."
fi

# Step 3: Install package
echo "[3/5] Installing package..."
if [ -d ".venv" ]; then
    .venv/bin/pip install -e . --quiet 2>/dev/null
    echo "       Package installed."
else
    echo "       ERROR: No .venv found!"
    exit 1
fi

# Step 4: Start
echo "[4/5] Starting trader..."
mkdir -p logs
nohup .venv/bin/python -m src.main > logs/trader.log 2>&1 &
TRADER_PID=$!
echo "       Trader PID: $TRADER_PID"
sleep 8

# Step 5: Test
echo "[5/5] Testing..."
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ 2>/dev/null || echo "000")
if [ "$RESPONSE" = "200" ]; then
    echo "       ✅ Dashboard is live at http://localhost:8000/"
else
    echo "       ⚠️  Dashboard returned HTTP $RESPONSE"
    echo "       Last 20 lines of log:"
    tail -20 logs/trader.log 2>/dev/null
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  Deploy complete!"
echo "  Logs: tail -f $DIR/logs/trader.log"
echo "═══════════════════════════════════════════"
