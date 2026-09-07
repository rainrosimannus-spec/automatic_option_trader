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

# ── Check portfolio login-form watcher ───────────────────
# Persistent helper that re-types credentials when the portfolio gateway drops to a blank login
# form (IBC can't inject them). Started by start-gateway-portfolio.sh; revive it here if it died.
if ! pgrep -u rain -f "portfolio-gw-autofill.sh" > /dev/null; then
    echo "$TIMESTAMP [WATCHDOG] portfolio autofill watcher missing — restarting" >> $LOGFILE
    nohup /home/rain/portfolio-gw-autofill.sh >/dev/null 2>&1 &
fi

# ── Reap orphaned gateway JVMs (nightly self-restart duplicates) ──
/home/rain/gw-orphan-reaper.sh

# ── Check trader (web dashboard + scheduler) ─────────────
if ! tmux has-session -t trader 2>/dev/null; then
    echo "$TIMESTAMP [WATCHDOG] trader session missing — restarting" >> $LOGFILE
    tmux new-session -d -s trader 'cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main'
    tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'  # persist stdout/stderr (uvicorn/web + crashes)
    echo "$TIMESTAMP [WATCHDOG] trader session started" >> $LOGFILE
    exit 0
fi

# Check if python process is running inside trader session
# NOTE: scope to user 'rain' — the son's nexbit trader runs an IDENTICAL
# `/home/nexbit/mesicap_trader/.venv/bin/python -m src.main`, so a bare
# `pgrep -f "python.*src.main"` matched HIS process and reported "trader OK"
# even when rain's trader was dead → auto-restart never fired.
if ! pgrep -u rain -f "python.*src.main" > /dev/null; then
    echo "$TIMESTAMP [WATCHDOG] python process dead — restarting trader session" >> $LOGFILE
    tmux kill-session -t trader 2>/dev/null
    sleep 2
    tmux new-session -d -s trader 'cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main'
    tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'  # persist stdout/stderr (uvicorn/web + crashes)
    echo "$TIMESTAMP [WATCHDOG] trader session restarted" >> $LOGFILE
else
    echo "$TIMESTAMP [WATCHDOG] trader OK" >> $LOGFILE
fi
