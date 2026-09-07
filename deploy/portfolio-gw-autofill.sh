#!/bin/bash
# portfolio-gw-autofill.sh — persistent login-form watcher for the portfolio gateway (:11 / :7496)
#
# WHY: IBC cannot inject credentials into the U26413485 gateway (the restarted process can't read
# /opt/ibc/config-portfolio.ini, so it "sets" an EMPTY username/password and stalls on the login
# form). Normally the nightly 23:45 restart re-authenticates via IBKR's stored restart token and no
# login form ever appears. When that token path fails (2026-09-06 23:45) the gateway drops to a blank
# login form and — with the old one-shot helper long exited — sat there for 14 hours.
#
# WHAT: loops forever (started by start-gateway-portfolio.sh, revived by watchdog-trader.sh) and
# types the credentials ONLY when the failure signature is present:
#     1. nothing is listening on :7496
#     2. an "IBKR Gateway" window exists on :11
#     3. no "Second Factor" dialog (already past the form -> waiting for the phone, don't retype)
#     4. IBC's log shows a "Setting password" line NEWER than its last "Login has completed" line
#        (the healthy token restart logs "Re-starting session" -> "Login has completed" and NEVER
#        logs "Setting password", so on a good night this watcher does nothing at all)
#     5. that "Setting password" line is >= GRACE seconds old (give IBC / a slow login a chance)
#
# RATE LIMITS (each successful login resets an "episode"): first 3 attempts >= 60s apart, then one
# attempt every 15 min. If an attempt reaches the 2FA push, back off 30 min before the next one so an
# overnight failure means a handful of IB Key pushes by morning, not one every 3 minutes.
#
# Credentials are read at RUNTIME (never stored in this script). Source order:
#   1) /home/rain/.portfolio-gw-creds   (rain-owned, chmod 600:  IBLOGIN=... / IBPASS=...)
#   2) /opt/ibc/config-portfolio.ini    (only if readable by this user)
set -u
export DISPLAY=:11
GWLOG=/home/rain/ibc/logs/portfolio
LOG="$GWLOG/autofill.log"
LOCK="$GWLOG/autofill.lock"
CREDS=/home/rain/.portfolio-gw-creds
CFG=/opt/ibc/config-portfolio.ini
PORT=7496
TICK=15            # seconds between checks
GRACE=45           # seconds a "Setting password" line must be old before we act on it
FAST_ATTEMPTS=3    # attempts allowed at 60s spacing per episode ...
FAST_GAP=60
SLOW_GAP=900       # ... then one every 15 min
TFA_GAP=1800       # after a 2FA push: 30 min before trying again
log(){ echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

# single instance — a second copy (start script re-run, watchdog) just exits
mkdir -p "$GWLOG"
exec 9>"$LOCK"
flock -n 9 || exit 0

IBLOGIN=""; IBPASS=""
if [ -r "$CREDS" ]; then . "$CREDS"; fi
if [ -z "${IBPASS:-}" ] && [ -r "$CFG" ]; then
  IBLOGIN=$(grep -m1 '^IbLoginId=' "$CFG" 2>/dev/null | cut -d= -f2- | tr -d '\r\n')
  IBPASS=$(grep  -m1 '^IbPassword=' "$CFG" 2>/dev/null | cut -d= -f2- | tr -d '\r\n')
fi
if [ -z "${IBLOGIN:-}" ] || [ -z "${IBPASS:-}" ]; then
  log "no credentials found (need $CREDS or a readable $CFG) -- abort"; exit 1
fi

port_up(){ ss -ltn 2>/dev/null | grep -q ":${PORT} "; }
gw_window(){ xdotool search --name "IBKR Gateway" 2>/dev/null | tail -1; }
tfa_window(){ xdotool search --name "Second Factor" 2>/dev/null | head -1; }

# epoch of the newest "Setting password" line if it is newer than the last "Login has completed";
# empty when the gateway is healthy / mid-token-restart / not yet at the form
stalled_login_epoch(){
  local f last_pw last_ok ts
  f=$(ls -t "$GWLOG"/ibc-*.txt 2>/dev/null | head -1); [ -n "$f" ] || return 0
  last_pw=$(grep -n 'IBC: Setting password' "$f" | tail -1)
  [ -n "$last_pw" ] || return 0
  last_ok=$(grep -n 'IBC: Login has completed' "$f" | tail -1 | cut -d: -f1)
  [ "${last_ok:-0}" -gt "${last_pw%%:*}" ] && return 0
  ts=$(echo "$last_pw" | cut -d: -f2- | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}')
  [ -n "$ts" ] && date -d "$ts" +%s 2>/dev/null
}

type_credentials(){   # $1 = attempt number
  local wid; wid=$(gw_window); [ -n "$wid" ] || return 1
  xdotool windowactivate "$wid" 2>/dev/null; xdotool windowraise "$wid" 2>/dev/null; sleep 1
  # Username field centre for the 790x610 login window at (117,79) on the 1024x768 :11 screen.
  # Even attempts fall back to the legacy click point (password field) that the one-shot helper
  # used and empirically logged in with on its 2nd/3rd try.
  if [ $(( $1 % 2 )) -eq 1 ]; then xdotool mousemove 532 357 click 1; else xdotool mousemove 512 410 click 1; fi
  sleep 0.4; xdotool key --clearmodifiers ctrl+a; xdotool key Delete; sleep 0.3
  xdotool type --delay 55 "$IBLOGIN"; xdotool key Tab; sleep 0.4
  xdotool key --clearmodifiers ctrl+a; xdotool key Delete; sleep 0.2
  xdotool type --delay 55 "$IBPASS"; sleep 0.4; xdotool key Return
}

log "watcher started (pid $$) — passive until :$PORT is down AND the login form is stalled"
attempts=0; last_attempt=0; last_tfa=0; was_up=0; noted_tfa=0
while true; do
  sleep "$TICK"
  if port_up; then
    if [ "$was_up" -eq 0 ]; then
      [ "$attempts" -gt 0 ] && log "logged in — :$PORT up after $attempts attempt(s)"
      attempts=0; last_attempt=0; last_tfa=0; noted_tfa=0; was_up=1
    fi
    continue
  fi
  was_up=0
  [ -n "$(gw_window)" ] || continue                    # gateway not (re)started yet
  if [ -n "$(tfa_window)" ]; then                      # creds accepted, phone approval pending
    [ "$noted_tfa" -eq 0 ] && { log "2FA dialog showing — awaiting phone approval"; noted_tfa=1; }
    [ "$last_tfa" -eq 0 ] && last_tfa=$(date +%s)
    continue
  fi
  noted_tfa=0
  stalled=$(stalled_login_epoch); [ -n "$stalled" ] || continue
  now=$(date +%s)
  [ $(( now - stalled )) -ge "$GRACE" ] || continue
  if [ "$last_tfa" -gt 0 ] && [ $(( now - last_tfa )) -lt "$TFA_GAP" ]; then continue; fi
  if [ "$attempts" -lt "$FAST_ATTEMPTS" ]; then gap=$FAST_GAP; else gap=$SLOW_GAP; fi
  [ $(( now - last_attempt )) -ge "$gap" ] || continue

  attempts=$(( attempts + 1 )); last_attempt=$now
  if type_credentials "$attempts"; then
    log "stalled login form detected (IBC 'Setting password' $(( now - stalled ))s ago) — typed credentials for $IBLOGIN (attempt $attempts)"
  else
    log "login window vanished mid-attempt (attempt $attempts)"; continue
  fi
  # give the login ~40s: either :7496 binds, a 2FA push goes out, or we retry on the next tick
  for i in $(seq 1 8); do
    sleep 5
    if port_up; then break; fi
    if [ -n "$(tfa_window)" ]; then last_tfa=$(date +%s); log "2FA reached (attempt $attempts) — creds accepted, IB Key push sent; next retry no sooner than $((TFA_GAP/60)) min"; break; fi
  done
done
