#!/bin/bash
export DISPLAY=:10
Xvfb :10 -screen 0 1024x768x24 &>/dev/null &
sleep 2

export TWS_MAJOR_VRSN=1037
export IBC_INI=/opt/ibc/config-options.ini
export TRADING_MODE=live
export TWOFA_TIMEOUT_ACTION=restart
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
