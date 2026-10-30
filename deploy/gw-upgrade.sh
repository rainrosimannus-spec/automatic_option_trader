#!/bin/bash
# Switch rain's two IB Gateways to 10.50 (installed under ~/ibkr/ibgateway/1050, IBC 3.24.2 at
# ~/ibc-3.24.2) and relaunch everything. Two IB Key pushes follow — approve both on the phone.
# Rollback: deploy/gw-rollback.sh (puts the 10.37 launchers back and relaunches).
set -e
cd "$(dirname "$0")/.."
for f in start-gateway-options.sh start-gateway-portfolio.sh; do
    [ -f ~/$f.1037 ] || cp ~/$f ~/$f.1037          # keep the live 10.37 launchers for rollback
    cp deploy/$f ~/$f
done
grep -q "TWS_MAJOR_VRSN=1050" ~/start-gateway-options.sh && grep -q "TWS_MAJOR_VRSN=1050" ~/start-gateway-portfolio.sh
[ -x ~/ibkr/ibgateway/1050/ibgateway ] && [ -x ~/ibc-3.24.2/scripts/ibcstart.sh ]
echo "launchers now point at gateway 10.50 + IBC 3.24.2 — relaunching (approve two IB Key pushes)"
exec ~/restart-all.sh
