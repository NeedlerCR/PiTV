#!/usr/bin/env bash
# /opt/pitv/cec-cmd.sh <cec-client command string>
#
# Kills any running cec-client process before issuing a command.
# This is necessary because tv_menu.py and potentially other processes
# hold the CEC bus open, which causes commands to fail or be ignored.
#
# The tv_menu.py CEC listener will automatically respawn its own
# cec-client within ~1 second of being killed.
#
# Usage:
#   /opt/pitv/cec-cmd.sh 'on 0'
#   /opt/pitv/cec-cmd.sh 'standby 0'
#   /opt/pitv/cec-cmd.sh 'as'
#   /opt/pitv/cec-cmd.sh 'volup'
#   /opt/pitv/cec-cmd.sh 'voldown'
#   /opt/pitv/cec-cmd.sh 'mute'

set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "Usage: cec-cmd.sh '<cec-client command>'" >&2
    exit 1
fi

CEC_COMMAND="$1"

# Kill every cec-client process unconditionally.
# SIGKILL (-9) skips the graceful shutdown to ensure it's gone immediately.
# '|| true' prevents the script failing if nothing was running.
pkill -9 -x cec-client 2>/dev/null || true

# Brief pause to let the bus settle after the previous holder is gone.
sleep 0.5

# Issue the command. '-s' = single command mode, '-d 1' = minimal logging.
echo "$CEC_COMMAND" | cec-client -s -d 1
