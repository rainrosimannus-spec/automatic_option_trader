#!/bin/bash
# ============================================================
# Install the Options Trader as a macOS Launch Agent
# This ensures the trader auto-starts on login and restarts on crash.
#
# Usage:
#   chmod +x install_launchd.sh
#   ./install_launchd.sh
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.optionstrader.supervisor.plist
#   rm ~/Library/LaunchAgents/com.optionstrader.supervisor.plist
# ============================================================

TRADER_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.optionstrader.supervisor"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

# Create logs dir
mkdir -p "$TRADER_DIR/logs"

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    
    <key>ProgramArguments</key>
    <array>
        <string>${TRADER_DIR}/supervisor.sh</string>
    </array>
    
    <key>WorkingDirectory</key>
    <string>${TRADER_DIR}</string>
    
    <key>RunAtLoad</key>
    <true/>
    
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    
    <key>StandardOutPath</key>
    <string>${TRADER_DIR}/logs/launchd_stdout.log</string>
    
    <key>StandardErrorPath</key>
    <string>${TRADER_DIR}/logs/launchd_stderr.log</string>
    
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
    
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF

echo "Created: $PLIST_PATH"

# Load the agent
launchctl load "$PLIST_PATH"
echo "Loaded launch agent. Trader will auto-start on login."
echo ""
echo "To check status:  launchctl list | grep optionstrader"
echo "To stop:          launchctl unload $PLIST_PATH"
echo "To restart:       launchctl unload $PLIST_PATH && launchctl load $PLIST_PATH"
