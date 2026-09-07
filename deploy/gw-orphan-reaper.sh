#!/bin/bash
# gw-orphan-reaper.sh — reap rain's ORPHANED IB Gateway JVMs (called from watchdog-trader.sh, cron */5)
#
# WHY: IBC is supposed to rename IB's own launcher so the gateway can't restart itself without IBC.
# On this box /opt/ibkr/ibgateway/1037/ is a symlink shim with no `ibgateway` file in it, so that
# rename silently finds nothing and the real launcher /opt/ibgateway/ibgateway stays live. Every
# 23:45 the gateway therefore spawns its OWN restart (`nohup /opt/ibgateway/ibgateway -J-Drestart=…`,
# re-parented to pid 1, no IBC) while IBC's loop ALSO relaunches — two JVMs for one single-use restart
# token. The loser sits on a blank login form forever (~500 MB each; 7 piled up Sep 3–7 2026, and the
# contention is the likely reason the token re-auth failed on Sep 6). Root fix = rename the launcher
# (needs sudo, and it is shared with the son's gateways); this reaper keeps the pile-up from growing.
#
# SAFETY RULES — a process is reaped only if ALL hold:
#   * owned by rain (never the son's identical nexbit processes)
#   * re-parented to pid 1 (IBC-managed JVMs have ibcstart.sh as parent — never touched)
#   * cmdline carries -J-Drestart= / -Drestart= AND one of rain's jtsConfigDir settings dirs
#   * older than MIN_AGE (a fresh self-restart may legitimately be the one that wins the port)
#   * NOT the process currently listening on 7496 or 4002 (whoever serves the port stays)
set -u
MIN_AGE=600   # seconds
LOGFILE=/home/rain/automatic_option_trader/data/watchdog.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

serving=$(ss -ltnp 2>/dev/null | grep -E ':(7496|4002) ' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)

ps -u rain -o pid= -o ppid= -o etimes= -o args= 2>/dev/null | while read -r pid ppid age args; do
  [ "$ppid" = "1" ] || continue
  case "$args" in *-Drestart=*) ;; *) continue ;; esac
  case "$args" in *jtsConfigDir=/home/rain/ibgateway-settings/*) ;; *) continue ;; esac
  [ "$age" -ge "$MIN_AGE" ] || continue
  echo "$serving" | grep -qx "$pid" && continue
  # the orphan is a `sh -c nohup …` wrapper (ppid 1) holding the java child: a plain kill of the
  # wrapper would leave the JVM running (no process-group kill), so take the children first
  kids=$(pgrep -P "$pid" | tr '\n' ' ')
  for k in $kids; do echo "$serving" | grep -qx "$k" && continue 2; done   # child serves a port → keep the pair
  [ -n "$kids" ] && kill $kids 2>/dev/null
  kill "$pid" 2>/dev/null
  echo "$TS [REAPER] killed orphan gateway pid=$pid children=[${kids% }] age=${age}s ($(echo "$args" | grep -oE 'ibgateway-settings/[a-z]+'))" >> "$LOGFILE"
done
exit 0
