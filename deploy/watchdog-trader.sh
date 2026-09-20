#!/bin/bash
# Watchdog: restart trader tmux session if Python process is dead
# Alerts if options/portfolio gateway sessions are missing (cant restart — needs 2FA)
LOGFILE="/home/rain/automatic_option_trader/data/watchdog.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# ── Check options gateway ────────────────────────────────
# RE-ENABLED 2026-06-11: dedicated options account is live (skxholdco / U25878705
# / port 4002). start-gateway-options.sh targets ~/ibc-config/config-options.ini.
# Relaunching the gateway needs IB Key 2FA approval on the phone to finish login;
# the trader's 5-min health check then reconnects once 4002 is open.
#
# PACED (2026-09-13): a cold launch sends an IB Key push. Hold until the newest push is
# TFA_GAP old — one push per 30 min, like portfolio-gw-autofill.sh — instead of the
# 5-in-30-min storm. The weekly 2FA is unavoidable (IBKR expires the auto-restart token on
# Sundays; the portfolio gateway needed one Sun 2026-09-13 22:31 too) — it is scheduled by
# ColdRestartTime in ~/ibc-config/config-options.ini, evaluated in the JVM's default zone =
# system UTC: 11:00 AM = 11:00 UTC = 13:00 Luxembourg in summer, 12:00 in winter.
TFA_GAP=1800
# Minutes since the newest "Second Factor Authentication initiated" line in the options IBC
# logs. IBC's JVM stamps those lines in the gateway's TimeZone (Europe/Luxembourg, jts.ini),
# not UTC. Prints nothing if there is no such line.
last_options_2fa_push_age_min() {
    local ts ep
    ts=$(grep -h "Second Factor Authentication initiated" /home/rain/ibc/logs/options/*.txt 2>/dev/null \
         | sed -E 's/^([0-9-]+ [0-9:]+):[0-9]{3}.*/\1/' | sort | tail -1)
    [ -n "$ts" ] || return 0
    ep=$(TZ=Europe/Luxembourg date -d "$ts" +%s 2>/dev/null) || return 0
    echo $(( ($(date +%s) - ep) / 60 ))
}
if ! tmux has-session -t options 2>/dev/null; then
    AGE=$(last_options_2fa_push_age_min)
    if [ -n "$AGE" ] && [ "$AGE" -lt $(( TFA_GAP / 60 )) ]; then
        echo "$TIMESTAMP [WATCHDOG] options gateway missing — IB Key push sent ${AGE} min ago, holding until $(( TFA_GAP / 60 )) min" >> $LOGFILE
    else
        echo "$TIMESTAMP [WATCHDOG] options gateway missing — restarting (last push ${AGE:-never} min ago)" >> $LOGFILE
        tmux new-session -d -s options '~/start-gateway-options.sh'
        echo "$TIMESTAMP [WATCHDOG] options gateway session started" >> $LOGFILE
    fi
    rm -f /home/rain/ibc/logs/options/api_down_since
else
    # Session PRESENT but the API port is down. Two benign cases: the 02:00-local daily
    # auto-restart (port down ~2 min) and a login in progress. One bad case (2026-09-20): the
    # Sunday cold restart (ColdRestartTime in ~/ibc-config/config-options.ini) relaunched the
    # gateway, the IB Key push was missed, and with ReloginAfterSecondFactorAuthenticationTimeout=no
    # IBC just SITS on the 2FA dialog — session alive, port dead, nothing ever relaunches: offline
    # until someone notices. So: once the port has been down TFA_GAP continuously AND the last push
    # is TFA_GAP old, kill this stuck instance and relaunch = one fresh push per 30 min until
    # approved. `api_down_since` (epoch) tracks the outage across these stateless 5-min runs; it is
    # seeded from the tmux session's creation time so a watchdog (re)start mid-outage still counts
    # from the real start, and cleared the moment the port is back.
    DOWN_FILE=/home/rain/ibc/logs/options/api_down_since
    if ss -ltn 2>/dev/null | grep -q ":4002 "; then
        rm -f "$DOWN_FILE"
    else
        if [ -s "$DOWN_FILE" ]; then
            DOWN_SINCE=$(cat "$DOWN_FILE")
        else
            DOWN_SINCE=$(tmux display -p -t options '#{session_created}' 2>/dev/null || date +%s)
            echo "$DOWN_SINCE" > "$DOWN_FILE"
        fi
        DOWN_MIN=$(( ($(date +%s) - DOWN_SINCE) / 60 ))
        AGE=$(last_options_2fa_push_age_min)
        if [ "$DOWN_MIN" -ge $(( TFA_GAP / 60 )) ] && { [ -z "$AGE" ] || [ "$AGE" -ge $(( TFA_GAP / 60 )) ]; }; then
            echo "$TIMESTAMP [WATCHDOG] options gateway session up but :4002 down ${DOWN_MIN} min (last push ${AGE:-never} min ago) — killing stuck instance and relaunching" >> $LOGFILE
            tmux kill-session -t options 2>/dev/null
            pkill -u rain -f "IbcGateway /home/rain/ibc-config/config-options.ini" 2>/dev/null
            sleep 3
            tmux new-session -d -s options '~/start-gateway-options.sh'
            echo "$DOWN_SINCE" > "$DOWN_FILE"   # keep counting from the original outage; a fresh push follows
            echo "$TIMESTAMP [WATCHDOG] options gateway session started" >> $LOGFILE
        elif [ "$DOWN_MIN" -ge 5 ]; then
            echo "$TIMESTAMP [WATCHDOG] options gateway :4002 down ${DOWN_MIN} min (last push ${AGE:-never} min ago) — waiting" >> $LOGFILE
        fi
    fi
fi

# ── Portfolio gateway: weekly Sunday recycle at 11:00 UTC ────────────────────
# IBKR invalidates the auto-restart tokens every Sunday 01:00 ET; the FIRST restart after that
# needs a full 2FA login (ibkrguides auto_restart_info). Left alone, that first restart is the
# nightly 23:45 UTC one = 01:45 Luxembourg Monday night — 2026-09-06 it fell to a blank login
# form for 14h. So recycle the gateway deliberately at 11:00 UTC Sunday (13:00 Luxembourg in
# summer, 12:00 in winter — the same slot as the options ColdRestartTime): the autofill watcher
# types the credentials, one IB Key push arrives at lunchtime, and the night restart then runs
# on a fresh token. Once per Sunday (date stamp in $PGW_RECYCLE), only inside the 11:00-11:09
# window, and only if the gateway is currently healthy (don't pile onto a login in progress).
PGW_RECYCLE=/home/rain/ibc/logs/portfolio/sunday_recycle_done
if [ "$(date -u +%u)" = "7" ] && [ "$(date -u +%H)" = "11" ] && [ "$(date -u +%M)" -lt 10 ] \
   && [ "$(cat "$PGW_RECYCLE" 2>/dev/null)" != "$(date -u +%F)" ] \
   && tmux has-session -t portfolio 2>/dev/null && ss -ltn 2>/dev/null | grep -q ":7496 "; then
    echo "$TIMESTAMP [WATCHDOG] portfolio gateway Sunday recycle — full re-login now (IB Key push follows) so tonight's 23:45 restart runs on a fresh token" >> $LOGFILE
    date -u +%F > "$PGW_RECYCLE"
    tmux kill-session -t portfolio 2>/dev/null
    pkill -u rain -f "IbcGateway /opt/ibc/config-portfolio.ini" 2>/dev/null
    sleep 3
    tmux new-session -d -s portfolio '~/start-gateway-portfolio.sh'
    echo "$TIMESTAMP [WATCHDOG] portfolio gateway session started" >> $LOGFILE
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
