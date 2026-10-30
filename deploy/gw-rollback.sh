#!/bin/bash
# Put the 10.37 launchers back (saved by gw-upgrade.sh) and relaunch everything. Two IB Key pushes.
set -e
for f in start-gateway-options.sh start-gateway-portfolio.sh; do
    [ -f ~/$f.1037 ] || { echo "no saved ~/$f.1037 — nothing to roll back"; exit 1; }
    cp ~/$f.1037 ~/$f
done
echo "launchers back on gateway 10.37 — relaunching (approve two IB Key pushes)"
exec ~/restart-all.sh
