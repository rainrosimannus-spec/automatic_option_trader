#!/bin/bash
export DISPLAY=:11
Xvfb :11 -screen 0 1024x768x24 &>/dev/null &
sleep 2

export TWS_MAJOR_VRSN=1037
export IBC_INI=/opt/ibc/config-portfolio.ini
export TRADING_MODE=live
export TWOFA_TIMEOUT_ACTION=restart
export IBC_PATH=/home/rain/ibc-3.24
export TWS_PATH=/opt/ibkr
export TWS_SETTINGS_PATH=/home/rain/ibgateway-settings/portfolio
export LOG_PATH=/home/rain/ibc/logs/portfolio
export JAVA_PATH=
export TWSUSERID=
export TWSPASSWORD=
export FIXUSERID=
export FIXPASSWORD=
export APP=GATEWAY
export HIDE=YES

mkdir -p /home/rain/ibgateway-settings/portfolio
mkdir -p /home/rain/ibc/logs/portfolio

# --- Smooth 2FA-free nightly auto-restart enrollment (self-heals on every launch) ---
# The gateway rewrites jts.ini while running, so seeding it live gets clobbered. This runs
# after restart-all.sh has killed the old gateway (it's DOWN here) and before relaunch, so
# the enrollment is guaranteed present when the new gateway reads the file. Mirrors the
# options gateway, which survives IBKR's ~00:00 UTC server restart without a 2FA prompt.
# Idempotent: each line is added only if missing / fixed only if wrong.
JTS=/home/rain/ibgateway-settings/portfolio/jts.ini
PGW_USER=fcjdcpkfdlaodoedmknaejncecnkpaoagnihkmfo   # portfolio (thenewroma/U26413485) username hash
if [ -f "$JTS" ]; then
    grep -q '^Restart=OK' "$JTS" || sed -i '/^\[Logon\]/a Restart=OK' "$JTS"
    sed -i 's/^Region=usr$/Region=us/; s/^Region=usr\r$/Region=us\r/' "$JTS"
    grep -q "^\[u:${PGW_USER}\]" "$JTS" || printf '\n[u:%s]\nAutoRestart=1\n' "$PGW_USER" >> "$JTS"
    echo "portfolio jts.ini auto-restart enrollment ensured"
fi

# Work around IBC's broken credential injection for U26413485: a persistent watcher types the
# login into the headless :11 form whenever it stalls blank — cold starts AND the nightly 23:45
# auto-restart when IBKR's token re-auth fails (2026-09-06: blank form for 14h). Passive on a
# healthy night. Kill any older copy first so an edited script actually takes effect (it's
# nohup'd and survives the tmux session, and it holds a flock so a stale one would win).
pkill -u rain -f 'portfolio-gw-autofill.sh' 2>/dev/null; sleep 1
nohup /home/rain/portfolio-gw-autofill.sh >/dev/null 2>&1 &

exec /home/rain/ibc-3.24/scripts/displaybannerandlaunch.sh
