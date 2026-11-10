#!/bin/bash
# Turn the assigned-lot stop OFF (wheel_stop_loss_enabled: false) and restart the trader.
# Counterpart of deploy/stop8.sh. Re-enable by deleting the line or running stop8.sh.
cd "$(dirname "$0")/.." || exit 1
if grep -q "^  wheel_stop_loss_enabled:" config/settings.yaml; then
  sed -i 's/^  wheel_stop_loss_enabled:.*/  wheel_stop_loss_enabled: false   # Rain: stop OFF (deploy\/stop-off.sh)/' config/settings.yaml
else
  sed -i 's/^  wheel_stop_loss_pct:.*/&\n  wheel_stop_loss_enabled: false   # Rain: stop OFF (deploy\/stop-off.sh)/' config/settings.yaml
fi
grep -n "wheel_stop_loss" config/settings.yaml
exec deploy/restart-trader.sh
