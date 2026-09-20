#!/bin/bash
# Rain's OPTIONS gateway (skxholdco -> U25878705, port 4002). Uses /opt/ibc/config-options.ini
# (Rain's own config — the son's separate gateway lives at /home/nexbit/ibc-config/).
export DISPLAY=:10
Xvfb :10 -screen 0 1024x768x24 &>/dev/null &
sleep 2

export TWS_MAJOR_VRSN=1037
# Private copy of the IBC config (2026-09-13): the /opt one is root-owned AND world-readable
# with IbPassword in it; this one is 0600 under ~. Two settings differ from /opt:
#   CommandServerPort=0  — IBC's command server was bound on every interface incl. the public
#                          IP, and a STOP on it killed this gateway at 07:00 UTC EVERY Sunday
#                          since 2026-03-22 (sender unidentified; nothing of ours uses the port).
#   ReloginAfterSecondFactorAuthenticationTimeout=no — one IB Key push per launch, not a
#                          re-push every ~10 min; the watchdog paces relaunches (30 min) and
#                          kills+relaunches an instance stuck on the 2FA dialog after 30 min.
#   ColdRestartTime=11:00 AM — IBC's weekly Sunday cold restart (IBKR expires the auto-restart
#                          token weekly, so this 2FA is unavoidable). It was 07:00 AM = 07:00 UTC
#                          = 09:00 Luxembourg Sunday, and THAT was the 'Sunday 07:00 kill' —
#                          not a STOP command. Evaluated in the JVM default zone (system UTC).
export IBC_INI=/home/rain/ibc-config/config-options.ini
export TRADING_MODE=live
# exit, not restart: with 'restart' the launcher re-logged in straight after a 2FA timeout
# (another push); with 'exit' the tmux session ends and watchdog-trader.sh relaunches it, but
# only once the last push is 30 min old — 5 pushes in 30 min on 2026-09-13 became 1.
export TWOFA_TIMEOUT_ACTION=exit
export IBC_PATH=/opt/ibc
export TWS_PATH=/opt/ibkr
export TWS_SETTINGS_PATH=/home/rain/ibgateway-settings/options
export LOG_PATH=/home/rain/ibc/logs/options
export JAVA_PATH=
export TWSUSERID=
export TWSPASSWORD=
export FIXUSERID=
export FIXPASSWORD=
export APP=GATEWAY
export HIDE=YES

mkdir -p /home/rain/ibgateway-settings/options
mkdir -p /home/rain/ibc/logs/options

exec /opt/ibc/scripts/displaybannerandlaunch.sh
