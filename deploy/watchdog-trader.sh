#!/bin/bash
# Watchdog: restart trader tmux session if Python process is dead
# Alerts if options/portfolio gateway sessions are missing (cant restart — needs 2FA)
LOGFILE="/home/rain/automatic_option_trader/data/watchdog.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# ── Check options gateway ────────────────────────────────
# RE-ENABLED 2026-06-11: dedicated options account is live (skxholdco / U25878705
# / port 4002). start-gateway-options.sh targets /opt/ibc/config-options.ini.
# Relaunching the gateway needs IB Key 2FA approval on the phone to finish login;
# the trader's 5-min health check then reconnects once 4002 is open.
if ! tmux has-session -t options 2>/dev/null; then
    echo "$TIMESTAMP [WATCHDOG] options gateway missing — restarting" >> $LOGFILE
    tmux new-session -d -s options '~/start-gateway-options.sh'
    echo "$TIMESTAMP [WATCHDOG] options gateway session started" >> $LOGFILE
fi

# ── Check portfolio gateway ──────────────────────────────
if ! tmux has-session -t portfolio 2>/dev/null; then
    echo "$TIMESTAMP [WATCHDOG] portfolio gateway missing — restarting" >> $LOGFILE
    tmux new-session -d -s portfolio '~/start-gateway-portfolio.sh'
    echo "$TIMESTAMP [WATCHDOG] portfolio gateway session started" >> $LOGFILE
fi

# ── Check trader (web dashboard + scheduler) ─────────────
if ! tmux has-session -t trader 2>/dev/null; then
    echo "$TIMESTAMP [WATCHDOG] trader session missing — restarting" >> $LOGFILE
    tmux new-session -d -s trader 'cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main'
    tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'  # persist stdout/stderr (uvicorn/web + crashes)
    echo "$TIMESTAMP [WATCHDOG] trader session started" >> $LOGFILE
    exit 0
fi

# Check if python process is running inside trader session
if ! pgrep -f "python.*src.main" > /dev/null; then
    echo "$TIMESTAMP [WATCHDOG] python process dead — restarting trader session" >> $LOGFILE
    tmux kill-session -t trader 2>/dev/null
    sleep 2
    tmux new-session -d -s trader 'cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main'
    tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'  # persist stdout/stderr (uvicorn/web + crashes)
    echo "$TIMESTAMP [WATCHDOG] trader session restarted" >> $LOGFILE
else
    echo "$TIMESTAMP [WATCHDOG] trader OK" >> $LOGFILE
fi
