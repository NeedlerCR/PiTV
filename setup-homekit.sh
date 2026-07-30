#!/usr/bin/env bash
# /opt/pitv/setup-homekit.sh
#
# The official Homebridge service is sandboxed (ProtectSystem=strict), which
# makes /tmp read-only for it. That stops homebridge-pitv-tv from writing the
# CEC power command to tv_menu's FIFO at /tmp/tv_menu.fifo — the symptom is:
#   cannot create /tmp/tv_menu.fifo: Permission denied
# (The FIFO itself is world-writable; the block is the systemd sandbox.)
#
# This installs a systemd drop-in that lets the Homebridge service share the
# real /tmp and write to it, then restarts Homebridge. Run once.
set -euo pipefail

DROPIN_DIR=/etc/systemd/system/homebridge.service.d
sudo mkdir -p "$DROPIN_DIR"
sudo tee "$DROPIN_DIR/pitv-cec.conf" >/dev/null <<'CONF'
[Service]
# Let homebridge-pitv-tv reach tv_menu's control FIFO at /tmp/tv_menu.fifo.
PrivateTmp=false
ReadWritePaths=/tmp
CONF

sudo systemctl daemon-reload
sudo systemctl restart homebridge

echo "Done."
echo "Now toggle the TV tile in the Home app and check:"
echo "  Homebridge log:  HomeKit -> TV ON (TV_ON -> /tmp/tv_menu.fifo)  (no 'Permission denied')"
echo "  Pi log:          grep 'HomeKit CEC' /tmp/pitv.log"
