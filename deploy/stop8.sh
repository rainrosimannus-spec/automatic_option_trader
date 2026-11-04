#!/bin/bash
# Step 2 of the 2026-11-04 bag exit: restore the normal assigned-lot stop (8%) and restart the trader.
# Usage (from the Claude prompt):  ! deploy/stop8.sh
cd "$(dirname "$0")/.." || exit 1
sed -i 's/^  wheel_stop_loss_pct: 0\.15.*/  wheel_stop_loss_pct: 0.08   # normal rule (restored by deploy\/stop8.sh)/' config/settings.yaml
grep -n "wheel_stop_loss_pct" config/settings.yaml
exec deploy/restart-trader.sh
