#!/bin/bash
# ============================================================
# IBC (IB Controller) Setup for macOS
# Auto-starts TWS and handles login, dialogs, and daily restarts
#
# Prerequisites:
#   - IBKR TWS OFFLINE version installed (NOT self-updating!)
#     Download from: https://www.interactivebrokers.com/en/trading/tws.php
#     → Choose "Offline (latest)" or "Offline (stable)"
#   - IBKR Mobile app for 2FA (recommended)
#
# Usage:
#   chmod +x setup_ibc.sh
#   ./setup_ibc.sh
#
# After setup, TWS will be managed by IBC:
#   Start:  ~/ibc/twsstart.sh
#   Stop:   ~/ibc/twsstop.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "═══════════════════════════════════════════"
echo " IBC Setup for Options Trader"
echo "═══════════════════════════════════════════"
echo ""

# ── Step 1: Detect TWS version ──────────────────────────────
echo "Detecting TWS installation..."

# macOS TWS installs to /Applications or ~/Applications
TWS_DIR=""
TWS_VERSION=""

for dir in "/Applications" "$HOME/Applications"; do
    for app in "$dir"/Trader\ Workstation\ *.app; do
        if [ -d "$app" ]; then
            TWS_DIR="$dir"
            # Extract version from app name (e.g., "Trader Workstation 10.30.app" → "10.30")
            TWS_VERSION=$(echo "$app" | sed 's/.*Trader Workstation //' | sed 's/.app//')
            echo "Found TWS $TWS_VERSION at $app"
            break 2
        fi
    done
done

if [ -z "$TWS_VERSION" ]; then
    echo ""
    echo "ERROR: TWS offline version not found!"
    echo ""
    echo "Please install the OFFLINE version of TWS:"
    echo "  1. Go to https://www.interactivebrokers.com/en/trading/tws.php"
    echo "  2. Download 'TWS Offline' for macOS"
    echo "  3. Install it"
    echo "  4. Run this script again"
    echo ""
    echo "IMPORTANT: IBC does NOT work with the self-updating TWS!"
    exit 1
fi

# ── Step 2: Download IBC ────────────────────────────────────
echo ""
echo "Downloading IBC..."

IBC_VERSION="3.19.0"
IBC_DIR="$HOME/ibc"
IBC_ZIP="/tmp/IBCMacos-${IBC_VERSION}.zip"

# Create IBC directory
mkdir -p "$IBC_DIR"

# Download
if [ ! -f "$IBC_ZIP" ]; then
    curl -L -o "$IBC_ZIP" \
        "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCMacos-${IBC_VERSION}.zip"
fi

# Extract
unzip -o "$IBC_ZIP" -d "$IBC_DIR"
chmod +x "$IBC_DIR"/*.sh 2>/dev/null || true
chmod +x "$IBC_DIR"/scripts/*.sh 2>/dev/null || true

echo "IBC installed to $IBC_DIR"

# ── Step 3: Create config.ini ────────────────────────────────
echo ""
echo "Creating IBC configuration..."

# Prompt for credentials
read -p "IBKR Username: " IBKR_USER
read -s -p "IBKR Password: " IBKR_PASS
echo ""
read -p "Trading mode (paper/live) [paper]: " TRADING_MODE
TRADING_MODE=${TRADING_MODE:-paper}

cat > "$IBC_DIR/config.ini" << EOCONFIG
# ============================================================
# IBC Configuration for Options Trader
# ============================================================

# Login credentials
IbLoginId=$IBKR_USER
IbPassword=$IBKR_PASS

# Trading mode: paper or live
TradingMode=$TRADING_MODE

# ── TWS Settings ──────────────────────────────────────────

# Accept incoming API connections without confirmation dialog
AcceptIncomingConnectionAction=accept

# Allow API connections from any host (for local use, localhost only)
IbAutoClosedown=no

# Don't show tips on startup
DismissNSEComplianceNotice=yes

# ── Auto-Restart ──────────────────────────────────────────

# Let TWS auto-restart daily (avoids full re-login)
# TWS restarts at this time to refresh connections
# Format: hh:mm (24h)
AutoRestartTime=01:00

# Auto logoff time — set very late so TWS runs all day
# TWS will restart (not log off) at AutoRestartTime instead
AutoLogoffTime=11:45 PM

# ── Two-Factor Authentication ─────────────────────────────

# If using IBKR Mobile 2FA:
# IBC will wait for you to confirm on your phone
# After first login of the week, auto-restart doesn't need 2FA again
SecondFactorAuthenticationExitInterval=60

# If 2FA times out, try again automatically
ReloginAfterSecondFactorAuthenticationTimeout=yes

# How many seconds to wait before retrying after 2FA timeout
SecondFactorAuthenticationRetryInterval=120

# ── Dialog Handling ───────────────────────────────────────

# Automatically handle common dialogs
ExistingSessionDetectedAction=primary

# Don't let another login kick us off
OverrideTwsApiPort=
OverrideTwsSocketPort=

# Suppress API precaution warnings
BypassOrderPrecautions=yes
BypassBondWarning=yes
BypassNegativeYieldToWorstConfirmation=yes
BypassCalledBondWarning=yes
BypassSameActionPairTradeWarning=yes
BypassPriceBasedVolatilityRiskWarning=yes
BypassUSOptionsFeeWarning=yes
BypassResultsAreNotGuaranteedWarning=yes

# ── Logging ───────────────────────────────────────────────

# Log file location
LogComponents=never
EOCONFIG

# Secure the config file (contains password)
chmod 600 "$IBC_DIR/config.ini"
echo "Config saved to $IBC_DIR/config.ini (permissions: 600)"

# ── Step 4: Update TWS version in start scripts ─────────────
echo ""
echo "Configuring start scripts for TWS $TWS_VERSION..."

# macOS uses the version with period (e.g., "10.30" not "1030")
for script in "$IBC_DIR"/twsstart.sh "$IBC_DIR"/gatewaystart.sh; do
    if [ -f "$script" ]; then
        sed -i '' "s/TWS_MAJOR_VRSN=.*/TWS_MAJOR_VRSN=$TWS_VERSION/" "$script"
        sed -i '' "s|IBC_INI=.*|IBC_INI=$IBC_DIR/config.ini|" "$script"
        sed -i '' "s|IBC_PATH=.*|IBC_PATH=$IBC_DIR|" "$script"
    fi
done

# ── Step 5: Create convenience wrapper ───────────────────────
cat > "$SCRIPT_DIR/start_tws.sh" << EOWRAPPER
#!/bin/bash
# Start TWS via IBC (auto-login, dialog handling)
echo "Starting TWS via IBC..."
"$IBC_DIR/twsstart.sh" &
echo "TWS starting in background. Check \$IBC_DIR/logs/ for IBC logs."
echo "2FA: Check your IBKR Mobile app if prompted."
EOWRAPPER
chmod +x "$SCRIPT_DIR/start_tws.sh"

# ── Step 6: Update supervisor to start TWS first ────────────
echo ""
echo "Updating supervisor to manage TWS..."

cat > "$SCRIPT_DIR/supervisor_full.sh" << 'EOSUPERVISOR'
#!/bin/bash
# ============================================================
# Full Stack Supervisor
# Starts TWS (via IBC) + Options Trader + auto-restart on crash
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

IBC_DIR="$HOME/ibc"
VENV_PYTHON=".venv/bin/python"
MAIN_MODULE="src.main"
RESTART_DELAY=30
LOG_FILE="logs/supervisor.log"
TWS_CHECK_INTERVAL=60      # check TWS every 60 seconds
TWS_API_PORT=4001
MAX_TWS_RETRIES=3

mkdir -p logs

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

cleanup() {
    log "Full supervisor shutting down"
    # Stop trader
    if [ -n "$TRADER_PID" ] && kill -0 "$TRADER_PID" 2>/dev/null; then
        kill "$TRADER_PID"
        wait "$TRADER_PID" 2>/dev/null || true
    fi
    rm -f .supervisor.pid .trader.pid
    exit 0
}

trap cleanup SIGINT SIGTERM
echo $$ > .supervisor.pid

# ── Check if TWS is running ─────────────────────────────────
is_tws_running() {
    # Check if TWS API port is accepting connections
    nc -z 127.0.0.1 "$TWS_API_PORT" 2>/dev/null
    return $?
}

# ── Start TWS via IBC if not running ─────────────────────────
ensure_tws() {
    if is_tws_running; then
        return 0
    fi

    log "TWS not running on port $TWS_API_PORT — starting via IBC..."

    if [ -f "$IBC_DIR/twsstart.sh" ]; then
        "$IBC_DIR/twsstart.sh" &
        IBC_PID=$!
        log "IBC started (PID $IBC_PID), waiting for TWS..."

        # Wait up to 120 seconds for TWS to become available
        for i in $(seq 1 120); do
            if is_tws_running; then
                log "TWS is ready on port $TWS_API_PORT"
                return 0
            fi
            sleep 1
        done

        log "WARNING: TWS did not become available in 120 seconds"
        return 1
    else
        log "WARNING: IBC not installed. Run setup_ibc.sh first."
        log "Waiting for manual TWS start..."

        # Wait for TWS to appear (someone might start it manually)
        for i in $(seq 1 300); do
            if is_tws_running; then
                log "TWS detected on port $TWS_API_PORT"
                return 0
            fi
            sleep 1
        done
        return 1
    fi
}

# ── Main loop ────────────────────────────────────────────────
log "═══════════════════════════════════════════"
log "Full Stack Supervisor started"
log "═══════════════════════════════════════════"

TWS_RETRIES=0

while true; do
    # Ensure TWS is running
    if ! ensure_tws; then
        TWS_RETRIES=$((TWS_RETRIES + 1))
        if [ $TWS_RETRIES -ge $MAX_TWS_RETRIES ]; then
            log "ERROR: TWS failed to start after $MAX_TWS_RETRIES attempts"
            log "Waiting 5 minutes before retrying..."
            sleep 300
            TWS_RETRIES=0
        fi
        continue
    fi
    TWS_RETRIES=0

    # Start the trader
    log "Starting trader..."
    $VENV_PYTHON -m $MAIN_MODULE &
    TRADER_PID=$!
    echo "$TRADER_PID" > .trader.pid
    log "Trader started (PID $TRADER_PID)"

    # Monitor both TWS and trader
    while true; do
        # Check if trader is still running
        if ! kill -0 "$TRADER_PID" 2>/dev/null; then
            wait "$TRADER_PID" 2>/dev/null
            EXIT_CODE=$?
            rm -f .trader.pid

            if [ $EXIT_CODE -eq 0 ]; then
                log "Trader exited cleanly. Stopping."
                cleanup
            fi

            log "Trader crashed (exit code $EXIT_CODE). Restarting in ${RESTART_DELAY}s..."
            sleep "$RESTART_DELAY"
            break  # restart trader
        fi

        # Check if TWS is still running
        if ! is_tws_running; then
            log "WARNING: TWS connection lost! Stopping trader and restarting TWS..."
            kill "$TRADER_PID" 2>/dev/null
            wait "$TRADER_PID" 2>/dev/null || true
            rm -f .trader.pid
            sleep 10  # give TWS time to fully die
            break  # will restart TWS + trader
        fi

        sleep "$TWS_CHECK_INTERVAL"
    done
done
EOSUPERVISOR
chmod +x "$SCRIPT_DIR/supervisor_full.sh"

# ── Step 7: Create launchd plist for full stack ──────────────
PLIST_PATH="$HOME/Library/LaunchAgents/com.optionstrader.fullstack.plist"

cat > "$PLIST_PATH" << EOPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.optionstrader.fullstack</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/supervisor_full.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/logs/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/logs/launchd_stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOPLIST

echo ""
echo "═══════════════════════════════════════════"
echo " Setup Complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "Files created:"
echo "  $IBC_DIR/config.ini    — IBC config (credentials)"
echo "  $SCRIPT_DIR/start_tws.sh       — start TWS via IBC"
echo "  $SCRIPT_DIR/supervisor_full.sh — full stack supervisor"
echo "  $PLIST_PATH"
echo ""
echo "Quick start:"
echo "  ./start_tws.sh                 — start TWS only"
echo "  ./supervisor_full.sh           — start TWS + trader"
echo ""
echo "Auto-start on login:"
echo "  launchctl load $PLIST_PATH"
echo ""
echo "IMPORTANT:"
echo "  - First login requires 2FA via IBKR Mobile app"
echo "  - After that, TWS auto-restarts daily without 2FA"
echo "  - Password is stored in $IBC_DIR/config.ini"
echo "  - File permissions are set to 600 (owner-only read)"
echo ""
echo "TWS Version: $TWS_VERSION"
echo "Trading Mode: $TRADING_MODE"
