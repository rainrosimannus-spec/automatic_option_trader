#!/bin/bash
echo "Killing old sessions..."
tmux kill-session -t options 2>/dev/null
tmux kill-session -t portfolio 2>/dev/null
tmux kill-session -t trader 2>/dev/null
sleep 2

echo "Killing any remaining java/python processes..."
pkill -f "ibcalpha" 2>/dev/null
pkill -f "IbcGateway" 2>/dev/null
pkill -f "GWClient" 2>/dev/null
pkill -f "python.*src.main" 2>/dev/null
pkill -f "automatic_option_trader.*python" 2>/dev/null
pkill -f "python.*automatic_option_trader" 2>/dev/null
sleep 3

# Verify everything is dead
remaining=$(ps aux | grep -E "java.*ibgateway|python.*src.main" | grep -v grep | wc -l)
if [ "$remaining" -gt 0 ]; then
    echo "Force killing remaining processes..."
    pkill -9 -f "ibcalpha" 2>/dev/null
    pkill -9 -f "IbcGateway" 2>/dev/null
    pkill -9 -f "GWClient" 2>/dev/null
    pkill -9 -f "python.*src.main" 2>/dev/null
    sleep 2
fi

echo ""
echo "=== SPLIT MODE: portfolio gateway :7496 + options gateway :4002 ==="
echo ""

# Print the "approve on your phone" line WHEN the IB Key push actually goes out — i.e. when the
# gateway's "Second Factor Authentication" dialog appears on its Xvfb display — not at launch.
# The push is sent only after credentials are submitted, and on the portfolio gateway that is
# done by ~/portfolio-gw-autofill.sh (IBC's own Login click never takes there), ~20-30s after
# launch; a fixed message at launch had the phone buzzing after "Starting trader" (2026-09-09).
#   $1 display  $2 API port  $3 label  $4 max seconds to wait  $5 tmux session
wait_for_2fa() {
  local disp="$1" port="$2" label="$3" max="$4" sess="$5" t=0
  while [ "$t" -lt "$max" ]; do
    if ss -ltn 2>/dev/null | grep -q ":${port} "; then
      echo "    $label: logged in without 2FA (token re-auth) — :$port is up"; return 0
    fi
    if DISPLAY="$disp" xdotool search --name "Second Factor" >/dev/null 2>&1; then
      echo ">>> APPROVE IB KEY ON YOUR PHONE for $label NOW — push sent (${t}s after launch) <<<"; return 0
    fi
    sleep 2; t=$(( t + 2 ))
  done
  echo "    $label: no 2FA dialog and :$port not up after ${max}s — check: tmux a -t $sess; tail ~/ibc/logs/portfolio/autofill.log"
  return 1
}

echo "=== Starting portfolio gateway (thenewroma / U26413485) ==="
tmux new-session -d -s portfolio '~/start-gateway-portfolio.sh'
wait_for_2fa :11 7496 thenewroma 120 portfolio

echo ""
echo "=== Starting options gateway (skxholdco / U25878705 / :4002) ==="
tmux new-session -d -s options '~/start-gateway-options.sh'
wait_for_2fa :10 4002 skxholdco 90 options

echo ""
echo "=== Starting trader ==="
tmux kill-session -t trader 2>/dev/null
sleep 1
tmux new-session -d -s trader 'cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main'
tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'  # persist stdout/stderr (uvicorn/web + crashes)
sleep 10

echo ""
echo "=== Status ==="
ss -tlnp | grep -E "4001|7496|8080"
tmux ls
echo ""
echo "Done! All systems should be running."
