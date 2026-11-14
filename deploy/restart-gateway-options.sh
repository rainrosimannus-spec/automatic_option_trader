#!/bin/bash
# restart-gateway-options.sh — restart ONLY Rain's options IB Gateway (skxholdco / U25878705 / :4002).
#
# WHY (2026-11-12): after the gateway's own 02:00 auto-restart the session came back with a dead
# history farm — every reqHistoricalData timed out all day (Error 366), and get_stock_price() is
# history-based, so the whole options side had no stock prices (put scans 0 candidates, stop check
# and CC manager blind). The portfolio gateway on :7496 answered the same request in 0.2 s, so it is
# this session, not IBKR. A fresh login fixes it.
#
# Touches nothing else: the portfolio gateway (:7496), the son's gateways and the trader process all
# keep running. The trader's health check reconnects to :4002 by itself within ~1 min of it coming up.
# Approve the IB Key push on the phone when the script says so (token re-auth may skip it).
set -u
SETTINGS=/home/rain/ibgateway-settings/options

echo "=== Stopping options gateway (:4002) ==="
tmux kill-session -t options 2>/dev/null
# Only the options IBC loop and its JVM — matched on the options settings path, never the portfolio
# gateway's or the son's identical processes.
for pid in $(pgrep -f "ibcstart.sh.*tws-settings-path=${SETTINGS}"); do kill "$pid" 2>/dev/null; done
for pid in $(pgrep -f "java.*jtsConfigDir=${SETTINGS}"); do kill "$pid" 2>/dev/null; done
sleep 4
for pid in $(pgrep -f "java.*jtsConfigDir=${SETTINGS}"); do kill -9 "$pid" 2>/dev/null; done
sleep 2
if ss -ltn 2>/dev/null | grep -q ":4002 "; then
  echo "!!! :4002 still listening — old gateway did not die; check: pgrep -af jtsConfigDir=${SETTINGS}"
  exit 1
fi

echo "=== Starting options gateway (skxholdco / U25878705 / :4002) ==="
tmux new-session -d -s options '~/start-gateway-options.sh'

# Same wait as deploy/restart-all.sh: say "approve" only when the Second Factor dialog is actually up.
t=0
while [ "$t" -lt 120 ]; do
  if ss -ltn 2>/dev/null | grep -q ":4002 "; then
    echo "    options: logged in without 2FA (token re-auth) — :4002 is up"; break
  fi
  if DISPLAY=:10 xdotool search --name "Second Factor" >/dev/null 2>&1; then
    echo ">>> APPROVE IB KEY ON YOUR PHONE for options NOW — push sent (${t}s after launch) <<<"; break
  fi
  sleep 2; t=$(( t + 2 ))
done
[ "$t" -ge 120 ] && echo "    options: no 2FA dialog and :4002 not up after 120s — check: tmux a -t options"

echo ""
echo "When :4002 is up the trader reconnects on its own (log line ibkr_reconnected_successfully)."
echo "Verify the farm is back:  grep -c 'reqHistoricalData: Timeout' logs/trader.log   (count must stop growing)"
