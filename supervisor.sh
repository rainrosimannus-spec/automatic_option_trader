#!/bin/bash
# ============================================================
# Options Trader Supervisor
# Monitors the trader process and restarts it on crash.
# Also monitors TWS/IB Gateway connection availability.
#
# Usage:
#   chmod +x supervisor.sh
#   ./supervisor.sh
#
# To run in background:
#   nohup ./supervisor.sh > logs/supervisor.log 2>&1 &
#
# To stop:
#   kill $(cat .supervisor.pid)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Config
VENV_PYTHON=".venv/bin/python"
MAIN_MODULE="src.main"
RESTART_DELAY=30          # seconds between restart attempts
MAX_RAPID_RESTARTS=5      # max restarts within RAPID_WINDOW
RAPID_WINDOW=300          # seconds (5 min) — if too many restarts, back off
BACKOFF_DELAY=300         # seconds (5 min) to wait after too many rapid restarts
LOG_FILE="logs/supervisor.log"
PID_FILE=".supervisor.pid"
TRADER_PID_FILE=".trader.pid"

# Ensure logs dir exists
mkdir -p logs

# Save supervisor PID
echo $$ > "$PID_FILE"

# Track restart times for rapid restart detection
declare -a RESTART_TIMES=()

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

cleanup() {
    log "Supervisor shutting down"
    if [ -f "$TRADER_PID_FILE" ]; then
        TRADER_PID=$(cat "$TRADER_PID_FILE")
        if kill -0 "$TRADER_PID" 2>/dev/null; then
            log "Stopping trader (PID $TRADER_PID)"
            kill "$TRADER_PID"
            wait "$TRADER_PID" 2>/dev/null || true
        fi
        rm -f "$TRADER_PID_FILE"
    fi
    rm -f "$PID_FILE"
    exit 0
}

trap cleanup SIGINT SIGTERM

check_rapid_restarts() {
    local now
    now=$(date +%s)
    
    # Remove old timestamps outside the window
    local new_times=()
    for t in "${RESTART_TIMES[@]}"; do
        if (( now - t < RAPID_WINDOW )); then
            new_times+=("$t")
        fi
    done
    RESTART_TIMES=("${new_times[@]}")
    
    if (( ${#RESTART_TIMES[@]} >= MAX_RAPID_RESTARTS )); then
        return 0  # too many restarts
    fi
    return 1
}

log "═══════════════════════════════════════════"
log "Options Trader Supervisor started"
log "═══════════════════════════════════════════"

while true; do
    # Check for rapid restart loop
    if check_rapid_restarts; then
        log "WARNING: Too many rapid restarts (${#RESTART_TIMES[@]} in ${RAPID_WINDOW}s)"
        log "Backing off for ${BACKOFF_DELAY} seconds..."
        sleep "$BACKOFF_DELAY"
        RESTART_TIMES=()  # Reset after backoff
    fi
    
    # Record restart time
    RESTART_TIMES+=("$(date +%s)")
    
    log "Starting trader..."
    
    # Start the trader
    $VENV_PYTHON -m $MAIN_MODULE &
    TRADER_PID=$!
    echo "$TRADER_PID" > "$TRADER_PID_FILE"
    
    log "Trader started (PID $TRADER_PID)"
    
    # Wait for the trader process to exit
    wait "$TRADER_PID" 2>/dev/null
    EXIT_CODE=$?
    
    rm -f "$TRADER_PID_FILE"
    
    if [ $EXIT_CODE -eq 0 ]; then
        log "Trader exited cleanly (code 0). Not restarting."
        break
    fi
    
    log "Trader crashed (exit code $EXIT_CODE). Restarting in ${RESTART_DELAY}s..."
    sleep "$RESTART_DELAY"
done

cleanup
