#!/bin/bash
# Restart ONLY the trader (python -m src.main) — gateways untouched, no IB Key push.
# Same launch line as ~/restart-all.sh and ~/watchdog-trader.sh.
tmux kill-session -t trader 2>/dev/null
sleep 2
tmux new-session -d -s trader 'cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main'
tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'
echo "trader relaunched — wait ~40 s, then check http://127.0.0.1:8080"
