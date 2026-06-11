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
echo "=== Starting portfolio gateway (pohja359 / U17562704) ==="
echo ">>> APPROVE IB KEY ON YOUR PHONE for pohja359 <<<"
tmux new-session -d -s portfolio '~/start-gateway-portfolio.sh'
sleep 35

echo ""
echo "=== Starting options gateway (skxholdco / U25878705 / :4002) ==="
echo ">>> APPROVE IB KEY ON YOUR PHONE for skxholdco <<<"
tmux new-session -d -s options '~/start-gateway-options.sh'
sleep 35

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
